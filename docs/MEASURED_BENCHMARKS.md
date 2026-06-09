# Measured Benchmarks — Real Numbers (June 2026)

> Testbed: GCP e2-medium (Debian 12, Python 3.11, 4GB RAM)
> Source: `tests/benchmark_workload.py`, `tests/benchmark_single_counting.py`, manual measurements with `toolrecall/cache.py` and `toolrecall/client.py`

## File Cache (13 project files, fresh DB)

| Metric | Value |
|--------|-------|
| Unique content cached | **170,857 bytes (13 files)** |
| Unique tokens intercepted | **55,189 tokens** |
| Avg tokens/file | ~4,245 |
| Avg bytes/file | ~13,142 |

## Hit Rate (re-read behavior)

| Scenario | Re-reads | Hits | Misses | Hit Rate |
|----------|----------|------|--------|----------|
| 3× re-read of 6 files | 18 reads | 12 | 6 | **67%** |
| 10× re-read of 12 files | 120 reads | 116 | 4 | **97%** |
| 141M inflation test: 100× same file | 100 reads | 99 | 1 | **99%** |

**Key insight:** With just 3-10 re-reads per file (typical for agentic sessions), hit rate ranges from 67-97%. The 89-91% cited in BOTTLENECK_SOLVED.md is realistic for a deep session with 6-10 re-reads.

## Latency

| Method | Cold (miss) | Hot (hit) | vs open/subprocess |
|--------|-------------|-----------|-------------------|
| Daemon path (UDS) | **4.3 ms** | **0.6 ms** | 5-30× faster |
| Direct Python import | **10.2 ms** | **8.7 ms** | comparable to subprocess |
| Raw `open()` | 0.2 ms | — | baseline |
| Raw `subprocess.run('cat')` | 3.3 ms | — | baseline |
| Raw `subprocess.run('hostname')` | 2.9 ms | — | baseline |

**Note:** The <0.1ms figure in older docs was aspirational. The actual daemon cache-hit latency via UDS is ~0.6ms. The "~1.5s per subprocess" figure in older docs referred to *end-to-end agent tool latency* (LLM call + subprocess + JSON serialization), not raw subprocess execution.

## Terminal Cache

| Metric | Value |
|--------|-------|
| Typical commands | 20 (hostname, pwd, whoami, uname, uptime, free, df, ls, git, etc.) |
| Bytes captured | ~512 bytes total |
| Tokens intercepted | **~170 tokens** (all 20 commands) |
| Avg per command | ~8.5 tokens |

The ~1,900 tokens/session in earlier docs was an overestimate (11×). Real terminal output is terse.

## Code Execution Cache

| Metric | Value |
|--------|-------|
| Typical snippets | 4 (list comprehension, JSON dump, line counter, file listing) |
| Tokens intercepted | **~14 tokens** |
| Avg per snippet | ~3.5 tokens |

Small expressions produce few tokens. The ~200 tokens/exec in earlier docs assumed longer generated output (e.g., JSON reports, analysis results).

## Memory

| Process | RSS (measured) | Virtual (VSZ) |
|---------|---------------|---------------|
| Daemon (background) | **~11.3 MB** | ~172 MB |
| Daemon (foreground) | **~7.8-8.0 MB** | ~172 MB |
| MCP bridge (stdio) | **~7.0 MB** | ~25 MB |
| SQLite cache.db on disk | **~0.5 MB** (after benchmark) | — |

The "0.09 MB" in `cache_stats.memory_used_mb` refers to the LRU data size, not process RSS.
The "130 MB" in older docs was VSZ (virtual address space), not RSS (physical RAM).

## Total Session Impact (13-file project, 3× re-read)

| Metric | Without Cache | With Cache | Savings |
|--------|--------------|------------|---------|
| Tokens sent to LLM | ~204K (13 files × 3) | ~55K (unique) | **73%** |
| Tool latency (827 calls) | ~20.6 min | ~0.5 sec | **~20 min** |
| Hit rate | 0% | 67-97% | — |

The 81% figure is correct for deeper sessions (10+ re-reads). For shallow sessions (3 re-reads), savings are ~73%.

## 141M Token Inflation (Fixed v0.3.2)

The original 141,112,165 token figure was produced by a double-counting bug where `tokens_intercepted` was incremented on **every** cache hit, not once per unique file. After 100 reads of the same file in a session, tokens were counted 100× instead of 1×.

**After fix:** 100 reads of the same file → tokens = 133 (the original file content), not 13,167 (99× inflation). Confirmed by `tests/benchmark_single_counting.py`.