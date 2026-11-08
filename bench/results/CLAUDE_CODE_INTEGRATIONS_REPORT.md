# ToolRecall × Claude Code — measured

Claude Code 2.1.306, `claude -p`, Haiku, Linux. Costs are Claude Code's own
`total_cost_usd` (API-equivalent list price, not invoice-verified) unless stated;
"units" are list-price-weighted tokens (input 1, cache write 1.25, cache read 0.1,
output 5). Harnesses: [`bench/claude_code_integrations/`](../claude_code_integrations/).
Every live run used an isolated daemon, proxy, DB and HOME; the live instance was
never touched.

> **Verdict.** ToolRecall saves tokens for Claude Code when **the same headless task
> runs again**. On real SWE-bench tasks (Sonnet, official grading), with all fixes in
> this branch, **every rerun replayed completely — sessions of 3 to 21 turns — and cost
> $0**. The first run costs the same as native (+0.7%), so it **pays off from the second
> run**: −45% after 2 runs, −65% after 3, −72% after 4 (5 tasks, 20 runs per arm).
> Session length did not matter as long as the rerun was identical; what breaks replay
> is any change to the request — see the repeat scenarios. Replay returns the cached
> run's outcome: a rerun is a cheap repeat, not a new attempt. For interactive sessions
> the saving is ~0% (0 of 3,015 tool results in 193 local sessions are repeats, and
> Claude Code already handles unchanged re-reads and stale edits itself).

This report replaces an earlier version whose headline figures (−33% turns, −38% and
−58% prompt tokens) were constants in the benchmark script and a synthetic message
list priced without prompt caching; the one real run of that version showed the hook
arm costing +88% (n=1).

## 0. Head-to-head: native vs ToolRecall on real SWE-bench tasks

5 SWE-bench Lite `django/db/migrations` issues, Sonnet, graded with the official
hidden tests. Arm A = Claude Code straight to the API; arm B = the recommended
ToolRecall setup (isolated proxy with the `claude-code` profile + staleness hook,
dedup off). Same prompt, tool allowlist, fixed checkout path, base commit,
`PYTHONHASHSEED=0`, pinned MCP; 3 runs per task per arm, arm order alternating.
Billed cost = Claude Code's `total_cost_usd` with replayed calls counted as 0
(1h cache writes weighted 2×). Harness: `ab_swe.py`, `ab_rig.sh`, `ab_analyse.py`;
task set: `swe_dj/setup.sh`.

**Run 3 — all fixes (stream cache, tool-schema default, parallel tool-result order);
4 runs per task per arm:**

| Runs of the same task | Native (cumulative) | ToolRecall (cumulative) | Δ |
|---|---:|---:|---:|
| 1 (cold) | $0.624 | $0.628 | +0.7% |
| 2 | $1.136 | $0.628 | **−44.7%** |
| 3 | $1.787 | $0.628 | **−64.8%** |
| 4 | $2.249 | $0.628 | **−72.1%** |

| Task | Native turns | Native $/rerun | ToolRecall $/rerun | Rerun calls replayed |
|---|---:|---:|---:|---:|
| 17087 | 3 | 0.024 | 0 | 3/3 ×3 |
| 16595 | 4 | 0.034 | 0 | 3/3 ×3 |
| 16820 | 9 | 0.087 | 0 | 5/5 ×3 |
| 15996 | 9 | 0.057 | 0 | 7/7 ×3 |
| 15738 | 30 (ToolRecall's cached run: 21) | 0.340 | 0 | 20/20 ×3 |

- **All 15 reruns replayed every call** (114/114); survival at call 13 was 3/3.
- **Wall time −49% on reruns** (78 s → 40 s): model latency disappears; the tools —
  Django's test runner — still execute.
- **Turns:** a replayed rerun has exactly the turns of the run it replays (9.0 mean
  vs 11.2 native); the saving per rerun is the whole native cost of that session, so
  longer sessions save more dollars (15738: $0.34 per rerun).
- **Solved 16/20 vs 12/20 is not an accuracy gain:** ToolRecall's cold run solved
  15738 and the three replays inherit that; native never solved it. Replay freezes
  whatever the cached run did.
- One-off cost per task varied −23%…+81% between arms (n=1 each): run-to-run
  variance, not overhead — the five-task total is +0.7%.

**Run 2 — with the tool-schema fix** (current profile minus the tool-order fix):

| Scenario | Arm | Runs | Solved (1st runs only) | Outcome frozen (reruns) | Total $ | $/run | Turns | Calls replayed |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| One-off (run 1) | native | 5 | **4 / 5** | — | $0.547 | $0.109 | 9.8 | 0 / 43 |
| One-off (run 1) | ToolRecall | 5 | **3 / 5** | — | $0.652 | $0.130 | 11.6 | 0 / 49 |
| Reruns (runs 2–3) | native | 10 | — (sampled live) | — | $0.982 | $0.098 | 10.6 | 0 / 90 |
| Reruns (runs 2–3) | ToolRecall | 10 | — (inherited) | **10 / 10 (100%)** | $0.404 | $0.040 | 10.4 | **62 / 88** |
| All (reference) | native | 15 | (10/15 across trials) | — | $1.529 | $0.102 | 10.3 | — |
| All (reference) | ToolRecall | 15 | (9/15 across trials) | — | $1.056 | $0.070 | 10.8 | 62 / 137 |

- **Reruns −58.8%, overall −30.9%.** 8 of 10 reruns replayed every call ($0); the
  other two broke after call 2 because Claude Code returned the results of parallel
  `Read` + `Grep` calls in the other order. The tool-order fix (committed after this
  run) turns both recorded break points into hits; not yet re-measured.
- **One-off runs: no saving** (+19.2%, n=5). Nothing can replay on a first run; the
  difference is run-to-run variance plus the hook.
- **Wall time unchanged** (+1.6% on reruns): replay skips model calls, but the tools
  — here Django's test runner — still execute.
- **Replay freezes outcomes.** Solved counts on reruns reflect outcome freezing (10/10
  identical to run 1), not independent attempts. Quality must only be evaluated on
  first runs; reruns report cost reduction and outcome fidelity.

## 0b. Repeat scenarios: what breaks replay (measured, 60 runs)

5 SWE-bench Lite tasks run through 6 consecutive conditions per arm (`ab_scen.py`, 60 runs total, one run per task, arm and condition, so small differences are noise).

The first pass lost 13 of the 60 runs to an API rate limit (HTTP 429): all 12 for task 17087 and the native `--shuffle` rerun of 16820. Those 13 were rerun with the same driver and code (`4eaaed3`) and replace the errored rows; the other 47 are from the first pass. Task 17087 got a new, cold ToolRecall cache.

| Scenario | Native $ | ToolRecall $ | Δ Cost | Calls replayed | TR outcome | Replay held through |
|---|---:|---:|---:|---:|---:|---|
| **Cold run** (base r1) | $0.539 | $0.665 | +23% (noise) | 0 / 49 | 4/5 solved | 0/24, 0/10, 0/3, 0/8, 0/4 |
| **Identical rerun** (base r2) | $0.543 | $0.127 | **−77%** | **42 / 48** | **5/5 frozen** | all, all, all, 1/7, all |
| **Rerun, new commit** (docs commit) | $0.638 | $0.563 | −12% (noise) | 0 / 41 | 2/5 same as cold (live) | 0/24, 0/5, 0/4, 0/5, 0/3 |
| **Rerun, prompt edited** (+1 sentence) | $0.620 | $0.540 | −13% (noise) | 0 / 42 | 3/5 same as cold (live) | 0/24, 0/4, 0/4, 0/7, 0/3 |
| **Cold run, `--shuffle`** | $0.708 | $0.662 | −7% (noise) | 0 / 48 | 3/5 solved | 0/24, 0/10, 0/4, 0/7, 0/3 |
| **Rerun, `--shuffle`** (volatile seed) | $0.455 | $0.310 | **−32%** | **32 / 50** | **5/5 frozen** | 19/26, 6/10, 3/4, 1/7, all |

### Takeaways for loosening / normalizers:
1. **New commit = 0% replay.** An unrelated docs commit changes git status and recent commit SHAs injected into Claude Code's first prompt. It breaks replay immediately at Call 1. For CI on consecutive commits, an opt-in profile that leaves git status out of the key is the only path to reuse.
2. **Volatile test output (`--shuffle`) = 64% of calls replayed (−32% cost).** Even with randomized test seeds, replay survives the read/inspect/edit phase (19/26, 6/10, 3/4 turns) before the first random test output forks the conversation. Specialized shuffle normalizers have limited additional upside because seed differences also reorder test execution.
3. **Prompt edit = 0% replay.** Inherent: the prompt defines the task and must go live.

**Run 1 — before the tool-schema fix:** ToolRecall +16.6% overall (native $1.537,
ToolRecall $1.792, n=15 per arm), 19 of 100 rerun calls replayed. Cause: Claude
Code's `Bash` tool schema states the background-timeout default as 600000 or 1800000
ms per session (server-side, ~50/50), so call 1 of about half the reruns missed —
the previously unexplained ~13% first-call miss. `toolrecall keytrace` names it.

The hook fired only on one task, when the agent edited a file through a Bash
`python3` heredoc — its own change, which the hook cannot tell from external drift.

## 1. Forward-proxy replay of repeated runs

**Bug found.** The stream tee and stream replay only accepted OpenAI-style streams
(`data: [DONE]`). Anthropic streams end with a `message_stop` event, so every Claude
Code stream was discarded as "truncated": 0 of 9 streamed calls stored, 0 replays.
The October replay results ([../claude_code_replay/RESULTS.md](../claude_code_replay/RESULTS.md))
came from non-streaming fallback retries after cut-short streams in those rigs: their
logs show every streamed call discarded and every HIT on a non-streaming request.

**After the fix** (`_sse_complete()`, Anthropic deltas in the empty-stream guard,
error events never cached), using the `toolrecall hook claude ci-env` settings:

| Run (same commit, same fixture) | Calls | Replayed | Billed units | vs cold |
|---|---:|---:|---:|---:|
| Cold | 3 | 0 | 21,427 | — |
| 8 identical warm reruns (mean) | 3 | 2.75 | 833 | **−96.1%** |

- 7 of 8 warm reruns replayed fully (0 upstream calls), **0 of 8 missed at call 1**.
  The October data had ~13% call-1 misses; 16 captured first requests of identical
  runs all produced the same canonical key, so that miss rate did not reproduce.
- The partial run broke at an `ls -l` mtime crossing a minute boundary. `toolrecall
  keytrace` (new, opt-in `TOOLRECALL_KEY_TRACE_DIR`) names the first differing field
  of every miss; a changed source file was pinpointed the same way (file size 47 vs
  76 bytes in the tool output).
- Billed units come from transcript usage with replayed calls (message id already
  seen in an earlier run) counted as 0; the proxy usage log agrees (HIT rows only for
  fully replayed runs). Claude Code's `total_cost_usd` counts replayed calls as billed.
- Not re-measured on the stream path: the long multi-issue session (−18% / −65% with
  `PYTHONHASHSEED=0` in October). Expect it to stay bounded by the first
  non-deterministic tool output.

## 2. Staleness hook (`PostToolUse`)

Task: Read `calculator.py`, run a build script that rewrites it, change `compute()`,
run pytest. Two prompts: *guided* (the branch's original prompt, which says "inspect
the state") and *neutral* (no hint the build step touches the file). n=6 per arm,
arm order alternated.

| Hook version | Prompt | Base cost | Hook cost | Δ cost | Turns base → hook |
|---|---|---:|---:|---:|---|
| as shipped | guided | $0.00410 | $0.00450 | **+9.8%** | 7.7 → 7.8 |
| as shipped | neutral | $0.00378 | $0.00349 | −7.6% | 8.0 → 6.0 |
| fixed | guided | $0.00383 | $0.00378 | −1.2% (noise) | 7.3 → 6.5 |
| fixed | neutral | $0.00377 | $0.00340 | **−10.0%** | 8.0 → 6.0 |

- **Bug found:** the shipped hook raised a "STALENESS ALERT: re-read" after every
  agent `Edit`/`Write` to a file it had read (it never refreshed its record), which
  triggered an extra `Read`. Fixed: reads and agent edits both record the current
  content; drift is checked only after `Bash`.
- Where the neutral-prompt saving comes from: without the hook, Claude Code's own
  guard rejects the stale `Edit` ("File has been modified since read") and Claude
  re-reads — the wrong edit is never applied. The hook saves that one round trip.
- The daemon's stale list is global across agents; merging it could surface files
  this session never read. Now opt-in (`TOOLRECALL_CC_DAEMON_STALE=1`).
- Hook latency on the test machine: 100–130 ms per tool call.

## 3. Request-body dedup with `normalize_output`

**Real workloads — nothing to stub.** The branch's dedup (min 800 chars, keep-first)
replayed over real transcripts, with and without the normalizer:

| Corpus | Sessions | API calls | Tool results | Blocks stubbed |
|---|---:|---:|---:|---:|
| Interactive sessions | 25 | 2,384 | 1,786 | 0 |
| All sessions incl. benchmarks | 193 | 3,777 | 3,015 | 0 |

(A first pass reported 85 stubs in one session; all came from duplicated transcript
lines left by a resume, which are never re-sent on the wire.)

**Best case — a task built to repeat output.** Claude runs a failing pytest suite
three times (~1.35 KB output each, only timings differ); dedup on vs per-request
opt-out through the same proxy, n=6 per arm after a warm-up pair:

| Stub text | No dedup | Dedup | Δ cost | Output tokens |
|---|---:|---:|---:|---|
| "Duplicate content omitted (sha256…)… byte-identical" | $0.001258 | $0.001473 | **+17.1%** | 427 → 964 |
| "Same output as message N (differs only in timings…); last line: …" | $0.001386 | $0.001396 | +0.7% (noise) | 491 → 886 |

The old stub also claimed "byte-identical" for normalized matches, which was false;
the model repeated the claim and once answered "Not confirmed". The new stub states
what differs and keeps the output's own last line. Dedup removes ~300 tokens per
repeat, roughly what the model then spends writing about the collapsed runs.
**Recommendation: leave dedup off for Claude Code.**

## 4. Where interactive-session cost goes — `toolrecall audit claude-code`

25 local interactive sessions, 2,428 API calls:

| | Share of cost |
|---|---:|
| Fixed starting context (median 39,199 tokens), re-read on every call | 13.7% |
| — of which controllable: skill listing, instruction files, MCP/agent listings | ~2.3% |
| — rest: Claude Code system prompt + tool definitions | ~11.4% |
| Output tokens | 16.3% |
| Each 1,000 always-loaded tokens | ~0.32% |

Trimming instruction files is worth doing only at scale: the largest controllable
item (skill listing, ~4.3k tokens) was 1.2% of cost.

## Reproduce

```bash
cd bench/claude_code_integrations
BENCH_DIR=/tmp/tr-cc-bench bash repeat_replay.sh 8          # section 1
python3 hook_ab.py 0 6                                       # section 2
python3 offline_dedup.py ../.. ~/.claude/projects            # section 3, real corpus
bash dedup_live.sh 7                                         # section 3, live A/B
python3 first_call_capture.py ../.. 16                       # call-1 key stability (no API calls)
toolrecall audit claude-code                                 # section 4
```
