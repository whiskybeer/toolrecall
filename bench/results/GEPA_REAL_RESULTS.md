# GEPA fork tree — real optimizer loop + synthetic repeats (2026-10)

Two benchmarks, both billed per request on OpenRouter (`usage.cost`; a proxy
HIT replays the original body, bills $0 and counts as `cost_avoided`). Total
spend for everything on this page: **$0.37**.

## 1. Real GEPA loop (`bench/run_gepa_real.py`)

The real `gepa.optimize()` (gepa 0.1.4) with GEPA's own LLM reflection
proposer, a tool-calling agent (grep / read_file / list_dir over a pinned
revision of this repo, v0.8.21) and a real scorer (find a function's file,
definition line and first parameter; 6 train + 6 val questions; the answer
format is learned from feedback). Budget 60 metric calls per arm. Arms:

- **default**: GEPA defaults; tools execute; LLM calls straight to OpenRouter.
- **gepa-cache**: GEPA's own `cache_evaluation=True`. This is the **fair baseline**.
- **toolrecall**: gepa-cache, plus `ToolRecallGepaAdapter` (tool calls through
  the replay fork tree), plus task-LM calls through the ToolRecall forward proxy
  (hermetic rig, `bench/gepa_real_rig.sh`).

| Model | Arm | Runs | Best val score | Total billed (mean) | vs gepa-cache | LLM calls served from cache | Tool calls executed / requested |
|---|---|---|---|---|---|---|---|
| deepseek-v4-flash | default | 3 | 1.00 | $0.0145 | | 0 / 157 | 100 / 100 |
| deepseek-v4-flash | gepa-cache | 3 | 1.00 | $0.0142 | | 0 / 151 | 94 / 94 |
| deepseek-v4-flash | **toolrecall** | 3 | 1.00 | **$0.0073** | **−48%** | 94 / 152 (62%) | 44 / 95 (53% replayed) |
| mistral-small-3.2-24b | default | 2 | 0.92 | $0.0114 | | 0 / 140 | 74 / 74 |
| mistral-small-3.2-24b | gepa-cache | 2 | 0.83 | $0.0109 | | 0 / 138 | 78 / 78 |
| mistral-small-3.2-24b | **toolrecall** | 2 | 1.00 | **$0.0109** | **+1%** | 49 / 162 (30%) | 75 / 107 (30% replayed) |

Per seed, toolrecall vs gepa-cache total billed: deepseek −33% / −68% / −65%;
mistral −11% / +18%.

### What the numbers say

- **No quality cost.** The best validation score was never lower with
  ToolRecall (5/5 runs ≥ the baseline arm). Mistral's 1.00 vs 0.83 is two runs
  of a noisy optimizer, not a quality claim.
- **Where the deepseek saving comes from: GEPA re-running identical rollouts.**
  GEPA evaluates the parent candidate on every iteration's minibatch with
  `capture_traces=True`. Its evaluation cache is written there but never read
  (gepa 0.1.4, `proposer/reflective_mutation/reflective_mutation.py`). Once
  deepseek found a perfect prompt early, most later iterations re-ran that same
  prompt on the same examples. The proxy served those LLM calls (62% HITs) and
  the fork tree served their tool calls. A GEPA-side fix (reading the cache for
  traced parent evals) would capture most of this saving without ToolRecall.
- **Across different prompts, only tool calls can be replayed.** A changed
  prompt changes every LLM request body. Mistral kept proposing new candidates
  (4–5 per run vs 2–3 for deepseek), so fewer rollouts repeated exactly: 30%
  LLM HITs, 30% tool replays, and no net dollar saving, because the toolrecall
  arm's trajectory happened to make more LLM calls (162 vs 138).
- **The tool axis is real but cheap here.** 30–53% of tool calls were served
  from the fork tree in every run. The tools in this bench are git reads
  (milliseconds, $0), so this shows up only as counts and wall time
  (deepseek: 129 s vs 290 s). With paid, slow or rate-limited tools, the same
  replay ratio would be worth more. That is unmeasured.
- **Reflection is out of reach.** GEPA's reflection calls were up to half of an
  arm's spend (deepseek seed 0: $0.0126 of $0.0229). They are unique prompts, so
  no cache layer can serve them.

### Caveats

- 5 runs (3 + 2 seeds), small task (12 questions), one task type. The spread
  per seed (−68% to +18%) is wide. Report a range, not one number.
- LLM sampling is not byte-deterministic, so arms explore different
  trajectories. Per-arm totals include that variance.
- A served HIT returns the earlier completion for an identical request. That is
  correct for temperature-0 evaluation. An optimizer that re-samples on purpose
  (temperature > 0, repeated scoring) would need the cache off for those calls.
- Single OpenRouter key: per-request `usage.cost` attribution; the per-key
  dashboard cross-check (G4) needs two capped keys.

Raw: `gepa_real_<model>_s<seed>.{json,jsonl}` (per arm summary + every request row).

## 2. Synthetic fork A/B, repeated (`bench/run_gepa_fork_ab.py`)

Same harness and workload as [GEPA_FORK_AB_RESULTS.md](GEPA_FORK_AB_RESULTS.md)
(3 SWE-bench Lite instances × B=5 scripted candidates × 8 turns, max_tokens=5),
repeated on two models, with a fresh hermetic rig per run.

| Model | Runs | Fork billed | Naive billed | Saving | Shared-prefix HITs | G5 byte-exact | Naive prompt tokens provider-cached |
|---|---|---|---|---|---|---|---|
| deepseek-v4-flash | 3 | $0.0043–0.0045 | $0.0253–0.0257 | 82.3–83.0% | 96/96 each | 25/25, 0 drift each | 97% |
| mistral-small-3.2-24b | 2 | $0.0077–0.0081 | $0.0451–0.0461 | 82.4–83.0% | 96/96 each | 25/25, 0 drift each | 0% |

- The September 79% reproduces at 82–83% with ±0.5 pt spread across 5 runs.
- It holds with and without provider prefix caching (deepseek 97% cached,
  mistral 0%). On this workload ToolRecall's saving is on top of the
  provider's.
- It is still the mechanism on a fixed shape (8 shared turns, scripted
  divergence, no task quality). Section 1 is the realistic number.

Raw: `gepa_fork_ab_v2_<model>_r<n>.{json,jsonl}`.

## Reproduce

```bash
# venv with gepa (any; the bench imports this checkout)
python3 -m venv /tmp/gepa-venv && /tmp/gepa-venv/bin/pip install gepa==0.1.4
RIG_DIR=/tmp/gepa_real_rig bash bench/gepa_real_rig.sh start
KEY=sk-or-... BENCH_DIR=/tmp/gepa_real_rig/adapter \
  /tmp/gepa-venv/bin/python -u bench/run_gepa_real.py --execute --seed 0 --budget 60
RIG_DIR=/tmp/gepa_real_rig bash bench/gepa_real_rig.sh stop
```
