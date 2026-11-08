# ToolRecall × Claude Code — handover: `/v1/messages` request-body dedup

**Revision 2** (incorporates the four review findings — see §9)
**Branch tip:** `1dd94ba` (+ working-tree fixes) · **Verified:** `make validate` green — 1609 passed, 46 skipped, 0 failed
**Evidence status:** mechanism-tested (15 tests incl. the protected-tail interaction). **No billed measurement exists on this path yet — the run produces the first one.**

## What's in the zip

| Path | What |
|---|---|
| `repo/` | The **complete source tree** at branch tip, changes applied — run/validate directly |
| `patch/` | The two commits as `git format-patch` (base `31b8b36`) |
| `run-arm.ps1` | Arm runner: sets per-terminal env, restarts the daemon, records start/end, calls the summarizer |
| `summarize_arm_window.py` | Per-arm summary from the proxy usage log (stdlib, no network) |
| `WORKLOAD.md` | The task prompt that reliably produces byte-identical tool output |
| `REVIEW-RESPONSE.md` | The review findings quoted point by point, each verdict and fix |
| `HANDOVER.md` | This file |

## 1. What changed

Request-body dedup now also fires on **Anthropic-shaped `/v1/messages` bodies** (Claude
Code's native path), not only `/v1/chat/completions`, and it **recurses into `tool_result`
content** — where a coding agent's repeated payload actually lives. Blocks are shallow-copied
and only the text leaf is replaced, so `cache_control` and `tool_use_id` survive byte-exact.

**One caveat that matters for this run:** prefix stability across turns holds only when a
duplicate is stubbed on its **first** send. The shipped default `protect_last=2` protects the
newest messages — and Claude Code's repeated file almost always arrives as the newest
`tool_result` — so it goes out in full, gets cached, and is stubbed one turn later. That
rewrites already-cached bytes and forces the whole suffix to be cache-written at ~1.25×
instead of read at ~0.1×. Keep-first already keeps the full copy, so the arms should run with
**`TOOLRECALL_DEDUP_PROTECT_LAST=0`**. Pinned by
`tests/test_dedup_anthropic.py::TestProtectedTailVsCache` (both directions).

## 2. Apply

The zip carries the full tree: use `repo/` directly, or copy it over an existing checkout.
`git am patch/*.patch` also works if your checkout is at `31b8b36`. Then run `make validate`
(it needs `uv`; the unit suite is ~2 min).

Sanity check:

```powershell
python -c "from toolrecall.proxy import _is_dedup_path; print(_is_dedup_path('/v1/messages'))"   # True
```

## 3. Environment — per terminal, never `setx`

`setx ANTHROPIC_BASE_URL` is machine-wide and would route **every** Claude Code session
(including the desktop app) through the proxy, mixing other sessions' rows into the arm's
window. Set it **in the arm's terminal only** — `run-arm.ps1` does this.

```powershell
$env:ANTHROPIC_BASE_URL = "http://localhost:8569"
$env:TOOLRECALL_FORWARD_DEDUP = "1"      # arm A; "0" for arm B
$env:TOOLRECALL_DEDUP_PROTECT_LAST = "0"
claude                                    # run from THIS terminal
```

**Verified:** the env var overrides `toolrecall.toml`, so the arms are authoritative even if
your config file already sets `dedup = true` (checked directly: `env=0 → disabled`,
`env=1 → enabled`).

## 4. The arms

| Arm | `TOOLRECALL_FORWARD_DEDUP` | `PROTECT_LAST` | Meaning |
|---|---|---|---|
| **A** | `1` | `0` | candidate: dedup on, prefix-safe |
| **B** | `0` | — | baseline: no dedup |

Run each arm in **its own terminal**, restarting the daemon so it picks the config up
(`toolrecall restart` — a health check plus clean restart). Do **not** use `toolrecall stop`
between arms: it also reverts the base-URL wiring. `setx` alone is not enough because it only
affects processes started afterwards.

*Optional third arm:* if you also want the size of the tail effect quantified, run the shipped
default (`PROTECT_LAST=2`) as **A2** and compare A vs A2 as well as A vs B.

## 5. The workload

See `WORKLOAD.md` for the prompt and preconditions. The headline correction from review:

- **`Read` is unusable as a duplicate source** — its output carries line-number prefixes, and
  recent versions collapse a re-read of an unchanged file into a short "file unchanged" note.
  `Read` + `Bash cat` therefore differ byte-wise.
- **Repeated `Bash` output is the reliable source** — the same command on unchanged input
  yields identical bytes. The prompt repeats a `cat` of a >1 KB file and a test run.
- Two `Read`s at different offsets do **not** create a duplicate: dedup's unit is the whole
  text block, not a substring.

## 6. Run and collect

```powershell
.\run-arm.ps1 -Arm A          # sets env, restarts daemon, starts the clock
claude                        # paste the WORKLOAD.md prompt; >= 8 turns
.\run-arm.ps1 -Arm A -End     # records the end, writes arm-A-summary.json

# second terminal:
.\run-arm.ps1 -Arm B
claude
.\run-arm.ps1 -Arm B -End

.\run-arm.ps1 -Compare
```

**Dry run first (3 turns, then `-End`):** if `dedup tokens = 0`, the workload produced no
byte-identical duplicate — fix the workload before spending money. Never report that as "0%".

Summarizer filters rows to `--host anthropic --path /v1/messages` so other sessions cannot
contaminate a window (`--no-filter` to disable).

## 7. Reading the numbers

Anthropic reports **three disjoint** input fields — compare the **total**:

| Metric | Expected in arm A | Meaning |
|---|---|---|
| `input_uncached` | down | full-price tokens removed |
| `cache_write` | down | ~1.25× write tokens removed |
| `cache_read` | ≈ flat | the kept earlier copy is what's cached — we keep the cheap one |
| **`prompt_total`** | down | input + cache_read + cache_write ← **compare this** |

- `dedup tokens = 0` → "no duplicate content; not measurable" — never "0%".
- **cache-read share collapses in arm A → STOP**: the rewrite broke prefix matching. With
  `PROTECT_LAST=0` that should not happen; if it does, it is a bug, not a result.
- **Cost:** `cost_usd` is provider-reported and Anthropic reports none, so it reads 0. Use the
  provider console for both windows; zeros are *unverifiable*, never savings.

## 8. Honest status and limits

- The earlier −32.3% / −30.0% figure is the **chat-completions** path (LiteLLM/OpenRouter);
  its own caveats note the hook cannot cover `/v1/messages` (`litellm#27518`). Do not carry
  that number over to this path.
- Nothing here is evidence about **MCP file caching** in Claude Code — that remains 2.4×
  *worse*, unchanged.
- **Expect a small number.** A harness that re-reads on purpose manufactures duplicates;
  Claude Code re-reads less, and may already collapse unchanged re-reads itself. A small or
  zero result is a legitimate, publishable outcome.

## 9. Review findings incorporated (revision 2)

| # | Finding | Resolution |
|---|---|---|
| 1 | `protect_last=2` defers a duplicate that arrives in the tail, then stubbing it rewrites cached bytes | **Confirmed in code** by the new `TestProtectedTailVsCache` (both directions). Arms now run `PROTECT_LAST=0`; an optional A2 arm measures the default's cost. Docs no longer claim unconditional prefix stability. |
| 2 | `Read` output has line prefixes, so `Read` vs `Bash cat` is not byte-identical; unchanged re-reads may be collapsed by Claude Code | §5 and `WORKLOAD.md` rewritten: repeated **Bash** output is the duplicate source; mandatory dry run before the A/B. |
| 3 | `setx ANTHROPIC_BASE_URL` routes every session through the proxy and contaminates the window | §3: per-terminal env only; `run-arm.ps1` sets it that way; the summarizer also filters by host/path. |
| 4 | Docs disagreed (config already `true` vs "off by default"; header toggle vs restart) | §3/§4 now consistent and verified: env overrides config (checked), both arms restart the daemon, the header remains a scripted-run toggle only. |
