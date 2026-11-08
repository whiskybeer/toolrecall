# Response to your review — point by point

Your four findings are quoted, then what changed. Verdicts are evidence-based: where you were
right, it is confirmed in code, not agreed politely.

## 1. `protect_last=2` breaks the prompt cache once per duplicate

> "In Claude Code, the repeated file almost always shows up in the newest tool_result, which is
> inside the protected tail. So on turn N it goes out in full, and Claude Code caches it (its
> cache marker sits on the last message). On turn N+1 that message leaves the protected tail and
> gets replaced with a stub. The earlier bytes change, the cached prefix stops matching from that
> message on, and everything after it has to be written to the cache again at 1.25x price."

**Confirmed in code.** The test you asked for now exists —
`tests/test_dedup_anthropic.py::TestProtectedTailVsCache`, both directions:

- default `protect_last=2`: nothing is stubbed while the duplicate sits in the tail, and on the
  turn it leaves the tail the prefix **changes** (`assertNotEqual` on the prefix — i.e. cached
  bytes get rewritten);
- `protect_last=0`: the stub lands on the duplicate's **first** send and the prefix stays stable
  (`assertEqual`).

Actions: the arms run with `TOOLRECALL_DEDUP_PROTECT_LAST=0`; an optional **A2** arm uses the
shipped default so the cost of the tail is *measured* rather than argued. The unconditional
"prefix stays stable across turns" claim is removed from `toolrecall/dedup.py`,
`toolrecall/proxy.py`, `docs/AGENT_COMPATIBILITY.md` and `CHANGELOG.md`.

## 2. The suggested workload may not produce any byte-identical duplicates

> "Claude Code's Read output has line-number prefixes, so Read and Bash cat on the same file
> won't give identical bytes. … Recent Claude Code versions already shorten re-reads of unchanged
> files to a brief 'file unchanged' note … It's worth a 3-turn dry run to confirm DEDUP: stubbed
> actually appears in the log before doing the full A/B."

**Correct — fixed.** `WORKLOAD.md` now makes repeated **raw `Bash` output** the duplicate source
and marks the 3-turn dry run **mandatory** (with the explicit rule that `dedup_tokens = 0` means
"change the workload", never "0% saving").

One addition to your list: "the same Read with different offsets that overlap" will not trigger
dedup either — the unit is the **whole text block**, so overlapping-but-different blocks are
never duplicates. Recorded in `WORKLOAD.md` so nobody retries it.

Your "check on your version" caveat is **unresolved from this side** — there is no Claude Code on
this machine. That is exactly what the dry run is for.

## 3. `setx ANTHROPIC_BASE_URL` routes every session through the proxy

> "That's risky, and other sessions would add rows to the usage CSV inside your measurement
> windows. It's better to set it only for the terminal that runs the arms."

**Correct — fixed.** `run-arm.ps1` sets the environment in the invoking terminal only and never
calls `setx`; the README says so explicitly. Defence in depth: the summarizer filters rows by
`--host api.anthropic.com --path /v1/messages` so foreign rows cannot enter a window even if a
session leaks. (A bare `--host anthropic` would also have matched `api.anthropictest.invalid` —
caught while testing the filter.)

## 4. Small inconsistencies in the docs

> "the protocol file says the live config already has dedup = true and uses the header toggle,
> while HANDOVER.md says dedup is off by default and uses a daemon restart. Arm B also needs a
> daemon restart, not just setx, because setx only affects processes started afterwards."

**Correct — fixed.** The separate protocol file is **dropped** — one authoritative doc now
(`HANDOVER.md`), so they cannot drift again. Both arms restart the daemon. Verified directly:
the env var **overrides** `toolrecall.toml` (`env=0` disables dedup even with `dedup = true`
configured), so the arms are authoritative either way.

---

## Your two questions, answered

**1. Where is the ToolRecall repo?** On the machine that produced this zip:
`/home/hermes/toolrecall` (branch `feature/anthropic-messages-dedup`, built in the worktree
`~/tr-wt/anthropic-messages-dedup`). For your side there is nothing to point at: **`repo/` in
this zip is the complete source tree** at the branch tip, so you can work from it directly. If
you already have a checkout, tell us its path and its HEAD — the patches in `patch/` apply with
`git am` on base `31b8b36`.

**2. `protect_last=0` and the new test — included, or the handover exactly as written?**
**Both included, and the default is deliberately unchanged.** The test is in the tree and
passing; the arms run `PROTECT_LAST=0`. The shipped default stays `2` for now, because that
default also governs the `/v1/chat/completions` path whose −32.3%/−30.0% figure was *billed*
with `protect_last=2` — changing it is a product decision that deserves its own measurement, not
a silent edit inside a handover. Arm A2 exists to price that decision.

## Your prep list → where each item lives

| Your step | In this zip |
|---|---|
| Apply the two patches + `make validate` | `repo/` (full tree) or `patch/` — validate green: 1611 passed, 0 failed |
| Add the missing test | `repo/tests/test_dedup_anthropic.py::TestProtectedTailVsCache` |
| Write `run-arm.ps1` | `run-arm.ps1` (also `repo/bench/anthropic_dedup/`) |
| A fixed workload prompt | `WORKLOAD.md` |
| A short dry run to confirm stubs fire | `WORKLOAD.md` §"Dry run first" + `run-arm.ps1 -Arm A -End` |
