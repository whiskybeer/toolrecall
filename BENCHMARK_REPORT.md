# ToolRecall Three-Arm Benchmark Report

**Generated:** 2026-07-22 15:10:18
**Model:** `deepseek-chat` via deepseek
**Arms:** naive, toolrecall
**Workloads:** bugfix
**Total runs:** 7 (1650 turns)

---

## Claims (locked before data collection)

| Claim | Statement | Proven by |
|-------|-----------|-----------|
| C1 | Baseline per-turn cost grows; ToolRecall's stays bounded | _(filled after analysis)_ |
| C2 | The gap widens with session length | _(filled after analysis)_ |
| C3 | Sessions of 340+ turns stay usable where baseline exhausts | _(filled after analysis)_ |
| C4 | Dropping clean files does not reduce task quality | _(filled after analysis)_ |
| C5 | Cache hit avoids subprocess fork (~112x) | _(filled after analysis)_ |

---

## Methodology

Three arms, interleaved per seed: naive → prefix → toolrecall → naive → ...

| Arm | Behaviour | What it measures |
|-----|-----------|------------------|
| naive | Full conversation history every turn. No dropping. | Worst-case token growth — the snowball problem |
| prefix | Full history every turn, same as naive. Relies on provider prefix caching. | Honest baseline — what you get from just having your provider do prefix caching |
| toolrecall | After each turn, drops content of clean (read-only) files from context. | ToolRecall's mechanism — bounded context growth |

**Key metric:** `request_tokens` — self-counted via tiktoken (cl100k_base) before every LLM call. This measures what the provider actually receives, not what it bills for.

---

## Results Summary

| Metric | naive | prefix | toolrecall | Best arm |
|--------|-------|--------|------------|----------|
| req_tok @ turn  10 |    37170 |        — |     4898 | toolrecall |
| req_tok @ turn  50 |   178607 |        — |    26547 | toolrecall |
| req_tok @ turn 100 |   298688 |        — |    55608 | toolrecall |
| req_tok @ turn 200 |   531355 |        — |   110248 | toolrecall |
| req_tok @ turn 340 |        — |        — |   175910 | toolrecall |
|---|---|---|---|---|
| **Median turns to exhaustion** |      200 |      200 | |
| **Estimated cost (total)** | $0.69299 | $12.41886 | |

---

## Wilcoxon Signed-Rank Test (on per-turn request_tokens)

_Paired by turn index across matched runs._

---

## Log-Rank Test (turns to exhaustion)
- **naive_vs_toolrecall**: chi²=0.87, p=0.3865 (not significant)

---

## Provider Prefix Caching Effect

_How much the provider's prefix caching reduces billed tokens vs what we self-counted._
- **naive**: request=173,578,285, provider_prompt=4,781,170, delta=-168,797,115 (-97.2%)
- **toolrecall**: request=85,175,625, provider_prompt=86,668,221, delta=+1,492,596 (+1.8%)


---

## Probe Recall

_Nonce recall rate by arm and lag. If recall drops with lag, context dropping causes amnesia._

| Arm | Lag | Recall rate | N |
|-----|-----|-------------|---|
| naive        |  30 | 0% | 18 |
| naive        | 130 | 0% | 6 |
| toolrecall   |  30 | 100% | 34 |
| toolrecall   | 130 | 100% | 18 |
| toolrecall   | 280 | 100% | 6 |


---

## Per-Arm Summary

```
            runs  total_turns  median_request_tokens  median_completion  total_api_time_s
arm                                                                                      
naive          3          600               296282.5                0.0       1627.198147
toolrecall     4         1050                71229.0              512.0      13719.489557
```

---

## Charts

- `fig1_context_growth.png` — per-turn request_tokens for all three arms
- `fig2_ratio.png` — prefix/TR ratio (gap widening with session length)
- `fig3_warmup.png` — tool cache hit rate over time

---

## Raw Data

Full turn_log is in `~/.toolrecall/benchmark.db`. Query:

```sql
SELECT arm, workload_id, turn_index, request_tokens, prompt_tokens, completion_tokens,
       cache_read_tokens, ctx_dropped_tokens_cum, status
FROM turn_log
ORDER BY run_id, turn_index;
```

---

## Reproduction

```bash
cd ~/toolrecall && /tmp/bench-env/bin/python3 bench/interleave.py <workload> --seeds 7 --max-turns 400
```

Requirements:
- ToolRecall daemon running (`toolrecall status`)
- `/tmp/bench-env` with tiktoken installed
- `OPENROUTER_API_KEY` in `~/.hermes/.env` (or `ANTHROPIC_API_KEY` for direct Anthropic)
- Schema applied (`migrations/002_turn_log.sql`)
