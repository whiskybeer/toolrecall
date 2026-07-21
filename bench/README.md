# ToolRecall Three-Arm Benchmark

Reproducible benchmark comparing three context management strategies for LLM agents:
**naive**, **prefix** (provider prefix caching), and **toolrecall** (context tracking + content dropping).

## Quick Start

```bash
# Install deps
python3 -m venv /tmp/bench-env
/tmp/bench-env/bin/pip install tiktoken numpy pandas matplotlib scipy

# Set API keys — use separate keys per arm for clean billing comparison
export NAIVE_API_KEY="sk-or-..."
export PREFIX_API_KEY="sk-or-..."
export TOOLRECALL_API_KEY="sk-or-..."

# Single-arm runs (1 seed, 30 turns)
cd ~/toolrecall && PYTHONPATH="~/toolrecall:$PYTHONPATH" /tmp/bench-env/bin/python3 bench/run_arm.py prefix review --seed 42 --max-turns 30

cd ~/toolrecall && PYTHONPATH="~/toolrecall:$PYTHONPATH" /tmp/bench-env/bin/python3 bench/run_arm.py toolrecall review --seed 42 --max-turns 30

# Interleaved run (alternates arms per seed for paired comparison)
cd ~/toolrecall && PYTHONPATH="~/toolrecall:$PYTHONPATH" /tmp/bench-env/bin/python3 bench/interleave.py review --seeds 3 --max-turns 200

# Dry run (tests plumbing, no API calls)
cd ~/toolrecall && PYTHONPATH="~/toolrecall:$PYTHONPATH" /tmp/bench-env/bin/python3 bench/interleave.py bugfix --seeds 1 --max-turns 5 --dry-run

# Generate charts and report
cd ~/toolrecall && /tmp/bench-env/bin/python3 bench/analyze.py
```

## The Three Arms

| Arm | Strategy | What it measures |
|-----|----------|------------------|
| **naive** | Full conversation history every turn. No dropping. | Worst-case — the snowball problem. Every file read accumulates forever. |
| **prefix** | Full history every turn, same as naive. Relies on provider prefix caching for savings. | Honest baseline — what you get from just having a provider with prefix caching (DeepSeek, Anthropic, OpenAI). |
| **toolrecall** | After each turn, drops clean (read-only) file content from all prior messages. Uses ToolRecall daemon's context tracker + dirty tracking. | ToolRecall's mechanism — bounded context growth. Clean files are stripped every turn. |

### Why three arms?

- **naive vs toolrecall**: Best case for TR — unlimited history, no prefix caching.
- **prefix vs toolrecall**: Realistic comparison — both can leverage provider prefix caching. The only difference is TR's context dropping.
- **Separate API keys per arm**: Each arm uses its own API key (`NAIVE_API_KEY`, `PREFIX_API_KEY`, `TOOLRECALL_API_KEY`) so **billed costs can be compared directly from the provider dashboard**, not from token-model estimates.

## Prerequisites

- **ToolRecall installed** (`pipx install toolrecall` or `pip install .`)
- **ToolRecall daemon running** (`toolrecall status`) — only needed for the `toolrecall` arm
- **API keys** — OpenRouter (or any OpenAI-compatible provider). Set via env vars:
  - `NAIVE_API_KEY` — used when arm=`naive`
  - `PREFIX_API_KEY` — used when arm=`prefix`
  - `TOOLRECALL_API_KEY` — used when arm=`toolrecall`
  - Falls back to `OPENROUTER_API_KEY` if no arm-specific key is set
- **Benchmark database** — auto-created at `~/.toolrecall/benchmark.db` (isolated from live daemon)
- **Python venv** with `tiktoken`, `numpy`, `pandas`, `matplotlib`, `scipy`

## Workloads

| Workload | Turns | Pattern | What it simulates |
|----------|-------|---------|-------------------|
| **bugfix** | 450+ | Mixed read/write — debug loop with file edits | Write-heavy agent session. High cache invalidation rate (every write dirties the file). |
| **feature** | 450+ | Moderate writes — add logging, utility functions | Balanced agent session. Some writes, some pure reads. |
| **analysis** | 400+ | Heavy reads, rare writes — count patterns, compare files | Read-mostly session. Low invalidation, high repeat-read ratio. |
| **review** | 200 | Same 4 core files every turn, no writes | ToolRecall's ideal case. Maximizes advantage of context dropping + cache hits. |

## Output

All results go to `~/.toolrecall/benchmark.db` (SQLite), isolated from the live daemon's cache.

After running, `analyze.py` generates:
- `fig1_context_growth.png` — context tokens per turn (all arms)
- `fig2_ratio.png` — prefix/TR ratio showing widening gap
- `fig3_warmup.png` — ToolRecall file cache hit rate over time
- `benchmark_stats.txt` — numerical statistics (exhaustion, cost estimates, growth rates)
- `BENCHMARK_REPORT.md` — standalone markdown report

### Key metrics

- **request_tokens** — self-counted via tiktoken (cl100k_base) before every LLM call. This measures what the provider receives.
- **prompt_tokens** — provider-reported billed tokens.
- **cache_read_tokens** — provider-reported prefix cache hits (0 on DeepSeek V4 Flash, non-zero on Anthropic/OpenAI).
- **ctx_dropped_tokens_cum** — cumulative tokens of file content ToolRecall dropped from context.
- **tool_cache_hits/misses** — ToolRecall file cache hit rate.
- **api_latency_s** — wall-clock time per turn (LLM call + file reads).

### Query raw data

```bash
sqlite3 ~/.toolrecall/benchmark.db

-- Per-arm summary
SELECT arm, COUNT(DISTINCT run_id) as runs, COUNT(*) as turns,
       ROUND(AVG(request_tokens)) as avg_req
FROM turn_log GROUP BY arm;

-- Per-turn detail (review workload)
SELECT turn_index, prompt_tokens, request_tokens,
       cache_read_tokens, ctx_dropped_tokens_cum
FROM turn_log WHERE workload_id='review' AND arm='toolrecall'
ORDER BY turn_index;
```

## Methodology

### Context dropping

ToolRecall's context tracker marks files as **clean** (read-only this turn) or **dirty** (written this turn). After each LLM response:
1. `context_set_checkpoint()` marks the turn boundary
2. All files read this turn are registered
3. After the response, `context_get_dirty()` returns which files were modified
4. **Clean files** (read but not written) are stripped from ALL prior messages
5. Only the instruction and response remain — file content is dropped until needed again

### Billing separation

Each arm reads its API key from a per-arm env var (`NAIVE_API_KEY`, `PREFIX_API_KEY`, `TOOLRECALL_API_KEY`). This lets you use **three separate OpenRouter accounts** (or API keys) and compare actual billed amounts from the provider dashboard — avoiding unreliable token-model cost estimates.

### Seed isolation

All arms in the same round use the **same random seed** for the workload (probes, file rotation), ensuring fair comparison. Only the arm logic differs.

## File Layout

```
bench/
├── README.md            ← this file
├── run_arm.py           — single-arm runner (naive | prefix | toolrecall)
├── agent.py             — per-arm agent turn logic + LLM calls
├── workloads.py         — workload definitions (bugfix, feature, analysis, review)
├── interleave.py        — multi-arm interleaved runner
├── analyze.py           — report/figure generator
├── probes.py            — recall-quality probe mechanism
├── turnlog.py           — SQLite logging schema
└── SKILL.md             — (local) Hermes Agent skill file
```

## Reproducing Published Results

To reproduce the exact published benchmark (prefix vs toolrecall, review workload):

```bash
# 1. Export two separate API keys
export PREFIX_API_KEY="sk-or-..."
export TOOLRECALL_API_KEY="sk-or-..."

# 2. Run prefix arm (1 seed, 30 turns)
cd ~/toolrecall && PYTHONPATH="~/toolrecall:$PYTHONPATH" \
  /tmp/bench-env/bin/python3 bench/run_arm.py prefix review --seed 42 --max-turns 30

# 3. Run toolrecall arm (1 seed, 30 turns)
cd ~/toolrecall && PYTHONPATH="~/toolrecall:$PYTHONPATH" \
  /tmp/bench-env/bin/python3 bench/run_arm.py toolrecall review --seed 42 --max-turns 30

# 4. Compare billed costs from OpenRouter dashboard
```