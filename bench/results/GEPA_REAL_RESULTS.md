# GEPA fork tree — real optimizer loop + synthetic repeats (2026-10)

Every number here is billed per request on OpenRouter (`usage.cost`). A proxy
HIT replays the original body, bills $0 and counts as `cost_avoided`. Total
spend for everything on this page: **$0.53**.

## 1. Real GEPA loop, equal work (`bench/run_gepa_real.py --design schedule`)

The real `gepa.optimize()` (gepa 0.1.4) with GEPA's own LLM reflection
proposer, a tool-calling agent (grep / read_file / list_dir over a pinned
revision of this repo, v0.8.21) and a real scorer. The task: find a function's
file, definition line and first parameter, on 6 train + 6 val questions; the
answer format is learned from feedback. Budget: 60 metric calls.

**Design.** One GEPA run with its own `cache_evaluation=True` (the fair
baseline any GEPA user gets for free) records every `evaluate()` call it makes:
prompt, example ids and traces flag. That schedule is then replayed call for
call in two arms:

- **baseline**: plain execution. Tools run, and LLM calls go straight to OpenRouter.
- **toolrecall**: `ToolRecallGepaAdapter` (tool calls through the replay fork
  tree), with agent LLM calls through the ToolRecall forward proxy (hermetic
  rig, `bench/gepa_real_rig.sh`).

Both arms run the same prompts on the same examples in the same order, so
ToolRecall is the only difference. The replay order alternates with the seed,
so neither arm always runs second against a provider prefix cache the other
warmed. Reflection runs once and costs the same for both arms.

| Model | Seed | Agent LLM billed: baseline → toolrecall | Saving | LLM calls served from cache | Tool calls replayed | Mean rollout score (base / TR) |
|---|---|---|---|---|---|---|
| deepseek-v4-flash | 0 | $0.0109 → $0.0060 | **45%** | 44% | 42% | 0.79 / 0.79 |
| deepseek-v4-flash | 1 | $0.0098 → $0.0022 | **78%** | 72% | 57% | 0.85 / 0.85 |
| deepseek-v4-flash | 2 | $0.0104 → $0.0027 | **74%** | 73% | 59% | 0.85 / 0.85 |
| mistral-small-3.2-24b | 0 | $0.0124 → $0.0089 | **28%** | 25% | 31% | 0.39 / 0.39 |
| mistral-small-3.2-24b | 1 | $0.0073 → $0.0058 | **21%** | 25% | 25% | 0.35 / 0.40 |
| mistral-small-3.2-24b | 2 | $0.0083 → $0.0023 | **72%** | 65% | 53% | 0.75 / 0.82 |

- **Agent LLM cost:** −65% on deepseek (mean of 3; range 45–78%) and −39% on
  mistral (mean of 3; range 21–72%). Saving is positive in 6/6 runs.
- **Including GEPA's reflection calls,** which ToolRecall cannot serve: the
  saving on the whole optimization is 29–71% (deepseek) and 17–70% (mistral).
  Reflection was 3–34% of the baseline's bill.
- **Tool calls:** 25–59% of tool calls served from the fork tree instead of
  executed.
- **Cross-check:** a second estimate inside the toolrecall arm alone
  (cost_avoided / (billed + cost_avoided)) gives 65% / 39% mean, matching the
  cross-arm numbers. Mean rollout scores are equal or higher with ToolRecall in
  every run.

### Why the saving varies by run

The saving tracks how often GEPA re-runs the same prompt on the same examples.
GEPA evaluates the parent candidate on every iteration's minibatch with
`capture_traces=True`. Its evaluation cache is written there but never read
(gepa 0.1.4, `proposer/reflective_mutation/reflective_mutation.py`), so those
rollouts repeat in full even with `cache_evaluation=True`. ToolRecall serves
them: the LLM calls from the proxy, the tool calls from the fork tree. Runs
that converge early and then mostly re-evaluate the same prompt (deepseek s1/s2,
mistral s2: few reflection calls, 3–12% of the bill) save 70%+. Runs that keep
proposing new prompts (mistral s0/s1) save 21–28%, mostly from tool replay
across prompts. A changed prompt changes every LLM request, so only tool calls
can be reused across different prompts.

A GEPA-side fix (reading its cache for traced parent evaluations) would remove
much of the repeated LLM work. The cross-prompt tool replay would remain.

### Caveats

- 6 runs, one small task type (12 code-navigation questions), two models.
  Report the ranges.
- LLM sampling is not byte-deterministic, so the two replay arms still differ
  slightly in agent steps (largest gap: 162 vs 146 calls, deepseek s1).
- The tools are git reads ($0, milliseconds). Avoided tool executions are
  reported as counts; what they are worth depends on the real tool.
- A HIT returns the earlier completion for an identical request. That is right
  for temperature-0 evaluation, but not if the optimizer re-samples on purpose.
- Single OpenRouter key: spend is attributed per request (`usage.cost`); no
  per-key dashboard cross-check.

Raw: `gepa_real_sched_<model>_s<seed>.{json,jsonl}`. The JSON holds the record
run (candidates, best prompt, reflection cost), both replay-arm summaries and
the replay order.

## 2. First design: independent arms (superseded)

`--design arms` runs three separate `gepa.optimize()` loops: GEPA default,
GEPA `cache_evaluation=True`, and that plus ToolRecall. Results:
`gepa_real_<model>_s<seed>.{json,jsonl}`.

| Model | Arm | Runs | Best val score | Total billed (mean) | vs gepa-cache |
|---|---|---|---|---|---|
| deepseek-v4-flash | gepa-cache | 3 | 1.00 | $0.0142 | |
| deepseek-v4-flash | + ToolRecall | 3 | 1.00 | $0.0073 | −48% |
| mistral-small-3.2-24b | gepa-cache | 2 | 0.83 | $0.0109 | |
| mistral-small-3.2-24b | + ToolRecall | 2 | 1.00 | $0.0109 | +1% |

**Why mistral showed +1%.** GEPA's reflection samples at temperature 0.7, so
independent arms evolve different prompts and do different amounts of agent
work. In both mistral runs, the ToolRecall arm's prompts made the agent search
more: 2.4–2.9 LLM calls and 1.5–2.0 tool calls per rollout, against 2.0–2.5 and
1.0–1.5 for the baseline. Inside that arm the cache still saved 20–40% of agent
LLM cost, but on a bigger workload. The cross-arm difference mixed ToolRecall's
saving with trajectory luck, which is why section 1 replays one schedule
instead.

## 3. Synthetic fork A/B, repeated (`bench/run_gepa_fork_ab.py`)

Same harness and workload as [GEPA_FORK_AB_RESULTS.md](GEPA_FORK_AB_RESULTS.md):
3 SWE-bench Lite instances × B=5 scripted candidates × 8 turns, max_tokens=5.
Repeated on two models, with a fresh hermetic rig per run.

| Model | Runs | Fork billed | Naive billed | Saving | Shared-prefix HITs | G5 byte-exact | Naive prompt tokens provider-cached |
|---|---|---|---|---|---|---|---|
| deepseek-v4-flash | 3 | $0.0043–0.0045 | $0.0253–0.0257 | 82.3–83.0% | 96/96 each | 25/25, 0 drift each | 97% |
| mistral-small-3.2-24b | 2 | $0.0077–0.0081 | $0.0451–0.0461 | 82.4–83.0% | 96/96 each | 25/25, 0 drift each | 0% |

The saving is the same with heavy provider prefix caching (deepseek) and with
none (mistral). This is the mechanism on a fixed best-case shape (8 shared
turns, scripted divergence, no task quality); section 1 is the realistic
number. Raw: `gepa_fork_ab_v2_<model>_r<n>.{json,jsonl}`.

## Reproduce

```bash
# venv with gepa (any; the bench imports this checkout)
python3 -m venv /tmp/gepa-venv && /tmp/gepa-venv/bin/pip install gepa==0.1.4
RIG_DIR=/tmp/gepa_real_rig bash bench/gepa_real_rig.sh start
KEY=sk-or-... BENCH_DIR=/tmp/gepa_real_rig/adapter \
  /tmp/gepa-venv/bin/python -u bench/run_gepa_real.py --execute --seed 0 --budget 60
RIG_DIR=/tmp/gepa_real_rig bash bench/gepa_real_rig.sh stop
```
