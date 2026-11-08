# ToolRecall × Claude Code — measured

Claude Code 2.1.306, `claude -p`, Haiku, Linux. Costs are Claude Code's own
`total_cost_usd` (API-equivalent list price, not invoice-verified) unless stated;
"units" are list-price-weighted tokens (input 1, cache write 1.25, cache read 0.1,
output 5). Harnesses: [`bench/claude_code_integrations/`](../claude_code_integrations/).
Every live run used an isolated daemon, proxy, DB and HOME; the live instance was
never touched.

> **Verdict.** ToolRecall saves tokens for Claude Code when **the same headless task
> runs again**: identical warm reruns cost **−96%** vs the cold run (short task, n=8).
> That only works since the Anthropic stream-cache fix in this branch: before it, the
> proxy discarded every Claude Code stream. For one-off and interactive sessions the
> saving is ~0%: there is no repeated tool output to remove (0 of 3,015 tool results in
> 193 local sessions), and Claude Code already handles unchanged re-reads and stale
> edits itself.

This report replaces an earlier version whose headline figures (−33% turns, −38% and
−58% prompt tokens) were constants in the benchmark script and a synthetic message
list priced without prompt caching; the one real run of that version showed the hook
arm costing +88% (n=1).

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
