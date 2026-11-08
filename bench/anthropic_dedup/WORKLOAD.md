# Workload — a task that reliably produces byte-identical tool output

**Why this file exists:** dedup only fires on content that appears **twice, byte-identically,
inside one request**. A generic "fix this bug" task may produce *zero* duplicates — and then
the A/B reports nothing regardless of how long it runs.

## Do NOT use these as the duplicate source

| Idea | Why it fails |
|---|---|
| `Read` the same file twice | Claude Code's `Read` output carries **line-number prefixes**; and recent versions collapse a re-read of an unchanged file into a short "file unchanged" note. Either way there is no byte-identical block. |
| `Read` + `Bash cat` on the same file | Different bytes: `Read` adds line numbers, `cat` does not. |
| Two `Read`s at different offsets | The whole tool_result differs, so the *block* is not a duplicate. Overlap does not help — dedup's unit is the whole text block, not a substring. |

## Use repeated `Bash` output instead

`Bash` returns raw stdout, so the **same command on unchanged input produces the identical
bytes** on every run. That is the reliable duplicate source.

**Preconditions before you start**

1. Pick a file the task does not modify, **larger than ~1,000 bytes** (the floor is 800 chars).
   Example: `src/main.py`, a lockfile, a data file, a long README.
2. Pick a command whose output exceeds ~1,000 bytes and is stable across runs — e.g. a test
   run, `git log --stat`, `ls -R`, a linter report.
3. **Do not edit any file during the run** — an edit changes the bytes and destroys the duplicate.

## The prompt (paste into Claude Code, both arms identically)

> Work through this in at least 8 explicit steps, and run each shell command as its own step.
> Do not modify any file in this repository — this is a read-only investigation.
>
> 1. `ls -R` the repository root.
> 2. `cat <BIG_FILE>` — full contents.
> 3. `grep -n "def \|class " <BIG_FILE>` — list definitions.
> 4. `cat <BIG_FILE>` again — full contents, to re-check something.
> 5. Run the test suite (`<TEST_CMD>`) and show the output.
> 6. Explain what the tests cover, in prose.
> 7. Run the same test suite again and show the output.
> 8. `cat <BIG_FILE>` one final time, then summarise what the file does.
>
> Use the Bash tool for every command. Do not use the Read tool for `<BIG_FILE>`.

Replace `<BIG_FILE>`, `<TEST_CMD>` with your real values.

## Expected duplicate inventory

| Turn | Command | Duplicates |
|---|---|---|
| 2 | `cat BIG_FILE` | first occurrence |
| 4 | `cat BIG_FILE` | **duplicate of turn 2** |
| 5 | test run | first occurrence |
| 7 | test run | **duplicate of turn 5** |
| 8 | `cat BIG_FILE` | **duplicate of turn 2/4** |

Each duplicate lands in the **newest** tool_result — which is exactly the case
`protect_last` handles, and the reason to run the arms with
`TOOLRECALL_DEDUP_PROTECT_LAST=0` (see HANDOVER §5).

## Dry run first (always)

Run **3 turns**, then confirm the mechanism actually fired before spending money on the full
A/B:

```powershell
python tools/summarize_arm_window.py --since <dry-run start> --until <dry-run end> --label dry
```

- `dedup tokens > 0` → duplicates were found and stubbed. Proceed.
- `dedup tokens = 0` → **the workload produced no byte-identical duplicate.** Do not run the
  A/B; change the workload (bigger file, more repeats) or use the desktop app's real traffic
  as the source. Reporting "0%" here would be a measurement artefact, not a result.
