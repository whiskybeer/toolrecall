# Claude Code — what ToolRecall can and cannot save (measured)

> **Correction (added later).** These replays were served from Claude Code's
> non-streaming fallback retries, not from the stream cache: the proxy's stream tee
> accepted only OpenAI-style `[DONE]` streams and discarded every Anthropic stream
> (`message_stop`), and the streamed attempts in these rigs were cut short, so Claude
> Code retried without streaming. The rig logs show every streamed call discarded and
> all HITs on non-streaming requests. With the stream tee fixed, streamed calls replay
> directly (short task: −96% on identical warm reruns, n=8). Conclusions about *when*
> replay pays off (same commit, deterministic output) still hold; the long-session
> figures have not been re-measured. See
> [../results/CLAUDE_CODE_INTEGRATIONS_REPORT.md](../results/CLAUDE_CODE_INTEGRATIONS_REPORT.md).

> **Verdict.** ToolRecall saves money in Claude Code only when **the same headless
> task is run again**. The condition is repetition, not session length: a one-off
> session — short or long — saves 0%, and interactive use saves nothing (Claude
> Code already handles redundant reads). On repeats, with the `claude-code`
> profile, short tasks cost 60–100% less and a 33–38-call session 18% less,
> because the first non-deterministic tool output ends replay for the rest of the
> session. Two conditions decide it: **same commit** (any new commit, even an
> empty one, changes the first request → 0 replay) and **deterministic tool
> output** (with `PYTHONHASHSEED=0` the 5-issue session went from −18% to −65%,
> with 2 of 3 repeats replaying all 38 calls). Good fit: benchmark/eval-harness
> iteration, agent regression tests, retries of the same CI commit. Not for evals that sample the model — replay
> returns the earlier answer.

Three questions, each answered with a measurement rather than an estimate:

1. Interactive sessions: is there redundant content left for ToolRecall to remove?
2. Headless repeats (CI, regression runs): does forward-proxy replay work for Claude Code?
3. If it works, how far does it go and what breaks it?

Claude Code 2.1.x, `claude -p` (Haiku for section 2, Sonnet for 2b), subscription (OAuth) auth. Costs are
**API-equivalent usage, not invoice-verified**: per-call `usage` from the session
transcript, weighted by list-price ratios (input 1, cache write 1.25, cache read 0.1,
output 5). A call replayed by the proxy never reaches the provider, so it counts 0.

## 1. Interactive sessions — nothing left to remove

Analysis of 76 local Claude Code transcripts (1,307 API calls):

| Where the cost goes | Share |
|---|---:|
| Cache reads of the context (avg ~100k tokens/call) | 68% |
| Output | 18% |
| Cache writes | 14% |
| All tool results combined (weighted by how long they stay in context) | ~15% |
| First-call baseline (system prompt + tools + instruction files) as share of all context tokens | 30% |

- **Unchanged re-reads: 0.** Claude Code dedups natively: an unchanged re-`Read`
  returns a ~90-char `file_unchanged` result (verified live). A `PreToolUse`
  read gate was prototyped (`feat/claude-code-read-gate`, not merged) and is redundant.
- **Identical Bash outputs: 0**, so request-body dedup has nothing to stub here.
- **Large Bash outputs:** head/tail truncation would cap at ~1.3%, and the large
  outputs were mostly deliberate `sed`/`grep` reads — truncating them would hurt.
- **Compactions: 1 in 76 sessions**; Claude Code re-attaches recent files itself.

## 2. Headless repeats (toy task) — replay works, but only with a canonical profile

Claude Code always streams. The proxy caches streams only for requests carrying a
canonicalization profile, so **without the header Claude Code gets 0 cache hits**.
Even with a profile, two identical runs differ in:

| Volatile field | Handling |
|---|---|
| `metadata.user_id` (account + session id) | `claude-code` profile drops `metadata` from the key |
| claude.ai connector tools load asynchronously (tool list 23 vs 28) | pin MCP: `--strict-mcp-config --mcp-config '{"mcpServers":{}}'` |
| Bash tool description flips between two server-side flag values (2 of 8 runs) | **kept in the key** — real tool text; costs one extra cache branch |
| Test-runner durations (`2 passed in 0.25s`) | `claude-code` profile → `in <duration>` |
| `ls -l` mtimes of a fresh checkout | **kept** — breaks the chain at that call |

Task: fix a one-line bug in a 2-function module, run pytest (3 API calls/run).
Fixture recreated at a fixed path before each run. Hermetic rig: own daemon,
socket, DB and `HOME`; the live instance was never touched.

| Arm (n=8 each) | Calls replayed | Runs fully replayed | Mean usage / run | vs cold run |
|---|---:|---:|---:|---:|
| `hermes` profile (generic UUID/timestamp rules) | 6 / 24 | 0 | 6,187 | −24% |
| `claude-code` profile | 16 / 24 | **4 / 8** | 3,137 | **−62%** |

Cold run (nothing cached) = ~8,180 units. All 16 runs fixed the bug (pytest green).
Partially replayed runs diverged exactly at the first non-deterministic tool output
(the `ls -la` mtimes); each new branch is stored, so later runs replay it in full.

**A direct-vs-proxy comparison is not used**, on purpose: the direct arm paid
provider cache writes the proxy arm did not (the proxy prefix had been warmed
by probe runs), and a non-default `ANTHROPIC_BASE_URL` changes Claude Code's
system prompt by ~350 tokens. Both effects are unrelated to ToolRecall and
would have shown a fake −59%. The comparison above is within one arm: cold vs warm.

Note: Claude Code's own `total_cost_usd` counts replayed calls as if billed
(it reads `usage` from the replayed response). Use the proxy's `HIT` rows or the
replayed-message-id method in `ab_report.py`, not `total_cost_usd`.

## 2b. Real tasks — SWE-bench Lite, Sonnet

Two SWE-bench Lite tasks (`pytest-dev__pytest-11148`, `pytest-dev__pytest-8906`),
Sonnet, `claude-code` profile, 5 runs per task through one fresh hermetic rig, fixed
work path, graded with the official hidden tests (`run_real.sh`, `report_real.py`).
All 20 runs resolved their task.

| Variant | Calls / run | Calls replayed in warm runs | Warm-run usage vs cold run |
|---|---:|---:|---:|
| Plain prompt (agent greps, edits, stops — never runs tests) | 3–4 | 28 / 28 | **−100%** |
| Prompt requires a repro + running the existing tests | 5–8 | 26 / 34 | **−69%** (−58% / −79% per task) |

With tests run, the chain broke at real non-determinism, never at a profile gap:

| Breaker | Where | Runs |
|---|---|---:|
| pytest's numbered temp dir (`/tmp/pytest-of-<user>/pytest-191/…`) in failure output | 11148, call 6 of 6–8 | 4 / 4 warm |
| `grep -r` output order differs between fresh checkouts (directory order) | 8906, call 2 | 1 / 4 warm |

Reading it honestly:
- **Replay held through the first 5 calls of every warm run but one** — reading,
  editing and the first test run. Everything after the first temp-dir path in a
  tool result was billed normally.
- **Cost saved < calls replayed** on 11148 (5 of 7 calls replayed, −58%): the
  replayed calls are the early, smaller-context ones.
- **"Overall −59% over 10 runs" is mostly 1 − 1/n**: one cold run per task is
  always paid in full. Quote the warm-run figure with its n.
- Candidate rule not added (would be tuning the profile to this bench): pytest
  temp-dir numbers. Directory-order differences cannot be normalized safely.
- Still short tasks (≤ 8 calls). Long sessions (30+ calls) are untested; expect the
  saving to come from the replayed prefix only.

## 2c. Long session — 5-issue backlog in one resumed session

Five SWE-bench Lite `django/db/migrations` issues worked through in **one** Claude
Code session (`claude -p`, then `--resume` per issue), Sonnet, tests run after
every edit, `claude-code` profile. 4 chains (1 cold + 3 warm), 33–38 calls each
(`run_chain.sh`, `report_chain.py`; grading with official hidden tests).

| Chain | Calls | Replayed | Usage vs the same calls unreplayed | Issues resolved |
|---|---:|---:|---:|---:|
| r1 (cold) | 38 | 0 | — | 3 / 5 |
| r2 | 33 | 8 | −18% | 3 / 5 |
| r3 | 33 | 8 | −17% | 4 / 5 |
| r4 | 33 | 8 | −17% | 3 / 5 |

**Warm chains: −18%** (vs own unreplayed calls). The "−34% vs the cold chain"
figure is not used: the cold chain happened to take 5 more calls.

Every warm chain replayed exactly the first 8 calls of issue 1, then diverged at
the same spot: a Python `set` printed by the agent's debug script iterates in a
different order per process (string hash randomization, `PYTHONHASHSEED`).

**Why long sessions save little:** every request carries the whole conversation.
After the first divergence, *all later requests in that session* are new keys —
issues 2–5 were never replayed, although they are the same tasks. Replay saves the
prefix before the first non-deterministic tool output, nothing after it.

Not tried (would be environment control, not a profile change): fixing
`PYTHONHASHSEED` in the CI environment.

## 2d. Safety controls for the `claude-code` profile — passed

Same gating idea as the `warp` profile (see `docs/CANONICALIZATION.md`), plus checks
aimed at this profile's own rules. Two single-issue SWE-bench Lite tasks
(`django__django-15996`, `django__django-17087`), Sonnet, tests run, one hermetic
rig, 22 runs (`ctl_run.sh`, `audit.py`). All 22 runs resolved their task.

| Control | Change vs the cached runs | Expected | Result |
|---|---|---|---|
| Positive | none (identical rerun) | replay | 4 of 6 warm reruns replayed **fully**; 2 missed from call 1 (cause not identified — safe direction) |
| Same prompt, other checkout dir | working directory only | 0 hits | **0 hits** (2/2) |
| Changed source, visible in `git status` | one comment line in `serializer.py` | 0 hits | **0 hits** (2/2) — git status is in the system prompt |
| Changed source, hidden from `git status` | same edit, `--assume-unchanged` | replay only until the edit is read | **1 hit each, then live** — the edit first appeared in the tool output right after call 1 |
| Other model | Haiku instead of Sonnet | 0 hits | **0 hits** (2/2) |
| Pass@1 | 6 fresh no-cache runs vs 8 replay-arm runs | equal | **6/6 vs 8/8 resolved** — delta 0 (ceiling: both tasks easy) |

**Replay audit (all experiments):** for each of the **79 replayed calls** (controls,
short tasks, long chains), the raw conversation before the call was diffed against
the run that produced the cached response. Every difference was one the profile
intends to ignore (test durations); **0 false matches**.

**Clean cost baseline:** the no-cache arm used the same proxy and base URL, so the
system prompt is identical in both arms. Warm replay runs vs fresh runs: **−61%**
(15996) and **−67%** (17087), n=3 per arm per task. The 2 call-1 misses are why
this is not −100%.

Known collision risks of the profile (by design, not observed as errors):

| Rule | Collision it allows |
|---|---|
| `metadata` dropped | identical requests from **different accounts** share an entry (the api_cache key never included credentials, for any profile) |
| durations normalized | a run where timing *is* the signal (perf work: "took 30s") replays an answer computed for a different timing |
| UUID / ISO timestamp placeholders (`hermes` rules) | a tool output whose UUID or timestamp is semantic replays across values |

## 2e. When replay pays off: commits and determinism

**New commit → no replay.** Claude Code sends a git summary with the first message
(branch, status, git user and the 5 most recent commits). `django__django-17087`,
Sonnet, one rig: an extra **empty** commit (no code change at all) → **0 hits**;
reruns of that same commit → full replay (4/4 calls, 13–15 s vs 25 s). The same holds
for a different git user (it is part of that summary). Not normalized on purpose:
the agent may legitimately use git history.

**Deterministic environment → long sessions replay fully.** The 5-issue chain from
2c, rerun with `PYTHONHASHSEED=0` in the agent's environment
(`CHAIN_EXTRA_ENV="PYTHONHASHSEED=0" run_chain.sh`), 4 chains:

| Chain | Calls | Replayed | Usage | Wall |
|---|---:|---:|---:|---:|
| r1 (cold) | 38 | 0 | full | ~8 min |
| r2 | 38 | **38** (all 5 issues) | **0** | ~5 min |
| r3 | 38 | 0 — missed at call 1 | full | ~8 min |
| r4 | 38 | **38** | **0** | ~4 min |

Warm chains: **−65%** (vs −18% without the fixed seed). Replay is bimodal: a repeat
either replays end to end or misses at the very first request. First-call misses
across all identical reruns with this profile: 4 of 31 (~13%); the system prompt and
context attachments were identical in every checked case, and a cached variant
re-matches on later runs (r4 matched r1/r2) — consistent with a request field that
alternates between a few values (likely the server-flag–dependent tool text seen in
section 2). Unidentified; a miss only costs money.

Audit: 85 more replayed calls (76 chain, 9 commit test), 0 false matches —
**164 audited in total**.

## 3. What this means

- Replay returns **the earlier answer**. Right for CI re-runs on unchanged inputs,
  harness regression tests, reproducible demos. **Wrong for evals that sample the
  model** (pass@k, variance studies): replay hides the variance you are measuring.
- The ceiling is set by tool-output determinism, not by ToolRecall: every
  time-dependent byte in a tool result forks the chain from that call on.
- **Savings shrink with session length**: −100% (short, no tests) → −69% (short,
  tests run) → −18% (5-issue session, 33–38 calls). One divergence ends replay for
  the rest of the session.
- Evidence size: one toy task (n=8, Haiku), two real short tasks (n=5 each,
  Sonnet), one 5-issue chain (n=4, Sonnet).

## Reproduce

```bash
RIG_DIR=/tmp/ccr-s1 bash bench/claude_code_replay/rig.sh   # hermetic daemon + proxy on :8590
bench/claude_code_replay/run_once.sh proxy proxy1   # repeat for proxy2..N
python3 bench/claude_code_replay/ab_report.py
RIG_DIR=/tmp/ccr-s1 bash bench/claude_code_replay/rig.sh stop
```

`capture.py` is a 400-returning sink that saves raw request bodies — used to diff
two runs and find the volatile fields above. It never forwards anything.
