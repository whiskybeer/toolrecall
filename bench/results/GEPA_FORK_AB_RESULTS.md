# Fork A/B — first billed run (2026-09-13)

## Result (3 instances × B=5 × 8 turns, deepseek/deepseek-v4-flash, edge :8573 → proxy :8572 → hermetic daemon)

| Arm | Billed | Shared-prefix turns | Shared HIT% | Divergent $ | Errors |
|---|---|---|---|---|---|
| fork (KEY_A, cache ON) | **$0.0004** | 96 | 96/96 = 100% | $0.0004 | 0 |
| naive (KEY_B, cache OFF) | **$0.0020** | 96 (re-billed) | 0% | $0.0005 | 0 |

- **Measured saving: 79%** (billing-verified per-request `usage.cost`; HIT rows billed $0)
- Modeled (TREE_SEARCH.md, turns-billed axis): 45 vs 13 turns = **71%** — measured 79% (per-turn cost is not uniform; the divergent tail turns are the cheap ones, the file-heavy prefix turns are expensive, so saving beats the flat-turn model)
- Correctness (G5, body-keyed): **25/25 HIT bodies byte-exact replays of their MISS, 0 drift** — C-F4 PASS
- Raw: `bench/results/gepa_fork_ab_20260913T120734Z.{json,jsonl}` (264 rows)

## Rig facts (needed to reproduce)

- The standalone proxy is a RELAY: cache checks go through `TransportClient` to the daemon socket
  (`/run/user/<uid>/toolrecall.sock` = prod). `TOOLRECALL_CACHE_DB` on the proxy is IGNORED for
  this path. Isolation requires a hermetic daemon (gate.py pattern):
  ```
  TOOLRECALL_TRANSPORT=/tmp/gepa_bench/sock TOOLRECALL_CACHE_DB=/tmp/gepa_bench/cache.db \
    python -m toolrecall daemon          # 1. hermetic daemon
  TOOLRECALL_TRANSPORT=/tmp/gepa_bench/sock ... TOOLRECALL_FORWARD_PORT=8572 \
    python -c "from toolrecall.proxy import run_forward_proxy; run_forward_proxy(...)"   # 2. proxy
  TOOLRECALL_TRANSPORT=/tmp/gepa_bench/sock ... \
    python -m toolrecall.adapters.warp --provider openrouter.ai --port 8573               # 3. edge
  ```
- HIT replays carry the ORIGINAL body verbatim — including its `usage.cost`. Every cost sum must
  use `billed_cost` (0 on HIT), never raw `total_cost`. (warp bench knew this; harness now does too.)
- deepseek-v4-flash is NOT byte-deterministic at temp=0/max_tokens=5 (identical bodies → different
  completions across arms). Cross-arm sha equality is NOT a correctness signal. G5 gates WITHIN-body
  HIT-vs-MISS replay identity only.
- Single shared key (prod sk-or key) for this run → G4 dashboard cross-check SKIPPED (concurrent
  prod spend pollutes the window). A public number needs two fresh capped keys so G4 runs.
- Cost cap $0.60/arm (unused). Total actual spend ≈ $0.0024 + smoke runs ≈ $0.003.

## Claims verdict (full run)

```
C-F1 fork cuts billed cost vs equal-work naive re-exploration: PASS — 79% (billing-verified)
C-F2 shared-prefix turns are free on the fork arm:             PASS — 96/96 HIT, $0.0000
C-F3 scaling with B:                                           MEASURED — naive $0.0004/cand, fork $0.0001/cand
C-F4 correctness (HIT = byte-exact MISS replay):               PASS — 25/25, 0 drift
G4 dashboard cross-check:                                      SKIPPED (single key)
```

## Honesty limits

- max_tokens=5: input-token economics only, not task quality (same caveat as every bench here)
- Simulated candidate mutations, not real LLM-proposed ones — measures the FORK MECHANIC, not
  optimizer quality
- Single key, single model, single run: no variance estimate yet. Repeat with 2 capped keys
  (G4 live) and 2 models before any public claim.
- The 79% is THIS workload's shape (C=8 shared turns, d=1 tail). Savings scale with C/d per the
  model; do not generalize the number.
