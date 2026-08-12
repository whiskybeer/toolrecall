# Dedup Hook Latency Report (p50/p95/p99)

**Date:** 2026-08-12 · **Repo:** toolrecall @ `f51a335` (+ Task 2)
**Question:** what does the LiteLLM dedup hook cost on the inference request path?
**Scope — what "latency" means here.** Two distinct numbers, reported separately:

1. **Added overhead of the hook** (the variable we control) — wall time of
   `dedup_messages` itself, isolated.
2. **End-to-end local-path delta** WITH vs WITHOUT dedup through a proxy on
   accumulating agent requests.

Total LLM round-trip back to a provider is **not** measured (no live provider
key in this environment). It is provider-dominated — any real
provider call runs in the tens-to-hundreds of ms range, so a sub-millisecond
dedup overhead is negligible there. That claim is reasoned from scale, not
fabricated from a measurement.

---

## Tier 1 — Microbenchmark: `dedup_messages` overhead (N=10k/shape)

Request shapes mirror the bimodal real profile (the CTO's whole-block-mismatch
point), run on GCP e2-medium via `time.perf_counter_ns()`:

| shape       | msgs | blocks | mean µs | p50 µs | p95 µs | p99 µs |
|-------------|------|--------|---------|--------|--------|--------|
| `small`     | 8    | none   | 5.6     | **4.3**| 7.7    | 12.1   |
| `dup_heavy` | 8    | yes    | 74.1    | **66.8**| 122.4 | 156.0  |
| `wrap_varied`| 9   | fewer  | 120.5   | **106.5**|199.7 | 250.6  |

- `wrap_varied` = same file bytes re-wrapped with line-number/path prefixes →
  breaks whole-block matches → FEWER dedup savings **and** slightly more work
  (every large block hashed, none stubbed). This quantifies the CTO's point:
  savings shrink when harness formatting varies; the cost stays small either way.
- Cost scales with **bytes hashed**, not message count (`dup_heavy` hashes
  3000-char blocks; `small`'s sub-800-char strings are never hashed).

**Microbench ceiling:** worst shape p50 ≈ **0.11 ms**, p99 ≈ 0.25 ms.

## Tier 2 — End-to-end local path: WITH vs WITHOUT dedup

Accumulating agent session (8 turns, 3 source files re-read across turns — the
real agent pattern where dedup fires), posted to a **local stdlib OpenAI-
compatible stub** (no provider network), 100 iterations:

| metric               | WITHOUT | WITH | Δ |
|----------------------|---------|------|-----|
| hook p50 (µs)        | —       | 189.1| — |
| hook p99 (µs)        | —       | 407.9| — |
| local path p50 (µs)  | 976.6   | 1007.0 | +30.4 |
| local path p99 (µs)  | 3636.7  | 1619.4 | noise |

- The local path is dominated by Python HTTP + JSON serialization (~1 ms), not
  the hook. The p50 delta (+30 µs) is inside run-to-run noise; p99 is pure
  HTTP/VM jitter, not dedup-attributable.
- Hook p50 on a realistic accumulating request (189 µs) is higher than the
  microbench's per-shape median because one request carries many KB of repeated
  file blocks being hashed — and is still **well under 1 ms**.

## The defensible claim

> ToolRecall's dedup hook adds **≤ ~0.2 ms (p50)** on the local request path
> for realistic accumulating requests — microbench worst-case p50 0.11 ms,
> full-request p50 0.19 ms. It is sub-millisecond in every configuration
> measured; end-to-end round-trip to a provider is provider-dominated and the
> dedup delta is negligible there.

## Files

- `bench/litellm_dedup/latency/microbench.py` + `fixtures.py` + `test_microbench.py`
- `bench/litellm_dedup/latency/microbench_results.json`
- `bench/litellm_dedup/latency/proxy_ab.py`
- `bench/litellm_dedup/latency/proxy_ab_results.json`

To re-run on **real** traffic: same harness, swap the accumulating fixture
for a real traffic slice.
