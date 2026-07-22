---
name: three-arm-benchmark
title: Three-Arm A/B Benchmark Runner
description: Run the three-arm interleaved benchmark (naive/prefix/toolrecall) with scripted workloads
---

# Three-Arm A/B Benchmark Runner

## When to use
Run the controlled three-arm benchmark — naive vs provider prefix vs ToolRecall — with scripted workloads on real DeepSeek API calls.

## Prerequisites
- ToolRecall daemon running: `toolrecall status` (check via `hermes terminal`)
- Venv with tiktoken: `/tmp/bench-env/bin/python3` — if missing, recreate:
  ```bash
  python3 -m venv /tmp/bench-env && /tmp/bench-env/bin/pip install tiktoken
  ```
- Codebase: `~/toolrecall/bench/` with `run_arm.py`, `agent.py`, `workloads.py`, `interleave.py`, `turnlog.py`, `probes.py`, `analyze.py`
- Schema applied: `turn_log + probe_result` tables must exist in `~/.toolrecall/cache.db`
- API key: `OPENROUTER_API_KEY` in `~/.hermes/.env`

## Quick start (dry-run — tests plumbing, no cost)

```bash
cd ~/toolrecall && /tmp/bench-env/bin/python3 bench/interleave.py bugfix --seeds 1 --max-turns 10 --dry-run
```

## Full benchmark run (default — OpenRouter / DeepSeek V4 Flash)

```bash
cd ~/toolrecall && /tmp/bench-env/bin/python3 bench/interleave.py bugfix --seeds 7 --max-turns 400
```

## Anthropic model via OpenRouter

```bash
cd ~/toolrecall && /tmp/bench-env/bin/python3 bench/interleave.py bugfix --seeds 3 --max-turns 200 --model "anthropic/claude-sonnet-4-20250514"
```

## Direct Anthropic API (requires ANTHROPIC_API_KEY)

```bash
cd ~/toolrecall && /tmp/bench-env/bin/python3 bench/interleave.py bugfix --seeds 3 --max-turns 200 --provider anthropic --model claude-sonnet-4-20250514
```

This runs 7 seeds × 3 arms = 21 runs interleaved (naive → prefix → toolrecall → naive → ...). Expect ~30-60 minutes depending on LLM latency.

## All workloads

| Name | Turns | Type |
|------|-------|------|
| `bugfix` | 450 | Read files → find bug → write fix. High write invalidation rate. |
| `feature` | 450 | Read docs → add feature → write tests. Medium write rate. |
| `analysis` | 400 | Read files → compare/extract statistics. Low write rate. |

## After the run

```bash
cd ~/toolrecall && /tmp/bench-env/bin/python3 bench/analyze.py --provider openrouter --model "deepseek/deepseek-v4-flash"
```

Adjust `--provider` and `--model` to match how the benchmark was run.

Generates:
- `fig1_context_growth.png` — request_tokens vs turn for all 3 arms
- `fig2_ratio.png` — prefix/TR ratio with bootstrap CI
- `fig3_warmup.png` — tool cache hit rate over time
- `benchmark_stats.txt` — Wilcoxon, log-rank, per-arm summary

## Reading the results

```bash
sqlite3 ~/.toolrecall/cache.db "
SELECT arm, workload_id, COUNT(*) as turns,
       AVG(request_tokens) as avg_req_tok,
       MAX(request_tokens) as max_req_tok
FROM turn_log
GROUP BY arm, workload_id
ORDER BY workload_id, arm;
"
```

## Probe recall check

```bash
sqlite3 ~/.toolrecall/cache.db "
SELECT arm, lag, AVG(passed) as recall_rate, COUNT(*) as n
FROM probe_result
GROUP BY arm, lag
ORDER BY arm, lag;
"
```

## Pitfalls
- The first run of each seed includes daemon warm-up (context tracker init, file cache priming). Discard first seed if warm-up noise matters.
- If ToolRecall daemon is not running, the toolrecall arm will time out on LLM call attempts via the daemon's client. Check with `toolrecall status` before starting.
- The benchmark calls the real LLM API and costs money. Estimate: ~$0.03-0.08 per full 21-run set on DeepSeek V4 Flash; much higher on Anthropic Claude Sonnet 4 (~$0.50-1.00).
- `--dry-run` mode uses a dummy agent that skips the LLM entirely. `request_tokens` is still counted (via tiktoken), but `prompt_tokens` will be 100 (stub). The toolrecall arm's context dropping is not exercised because dummy responses contain no file content to strip.
- For direct Anthropic API (`--provider anthropic`), set `ANTHROPIC_API_KEY` in the environment or `~/.hermes/.env`.
- When using Anthropic models via OpenRouter (`--model "anthropic/claude-..."` without `--provider anthropic`), the existing `OPENROUTER_API_KEY` is used. OpenRouter returns usage in OpenAI-compatible format.