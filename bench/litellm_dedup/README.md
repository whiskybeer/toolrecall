# LiteLLM Dedup Hook — Benchmark & Methodology

Billing-verified measurement of the `toolrecall.adapters.litellm` gateway dedup
hook on **real SWE-bench Lite agent workloads**.

## Headline result (billing-verified, OpenRouter)

On an **accumulating agent loop** — 10 SWE-bench Lite instances × 8 accumulated
turns = **80 requests per arm**, DeepSeek V4 Flash via OpenRouter — the hook cut:

| Metric (80 requests/arm) | WITH dedup | WITHOUT dedup | Saved |
|---|---|---|---|
| **Total prompt tokens** | 282,688 | 417,256 | **134,568 (−32.3%)** |
| **Billed cost (OpenRouter)** | $0.0134 | $0.0191 | **$0.0057 (−30.0%)** |
| **Effective $/M (billed)** | $0.0474 | $0.0459 | Δ $0.0015 |

**Prefix caching preserved.** Effective per-token rate is near-identical between
arms (Δ $0.0015/M) — the keep-first design stubs only *later* duplicates, so
each file block's first occurrence (and the message prefix before it) is
byte-identical to the non-dedup arm, and the provider's cacheable prefix is
never rewritten.

### Savings curve (aggregate, per turn)

| Turn | WITH | WITHOUT | Saved | % |
|---|---|---|---|---|
| 1 | 4,793 | 4,793 | 0 | 0.0% |
| 2 | 22,822 | 22,822 | 0 | 0.0% |
| 3 | 40,901 | 40,901 | 0 | 0.0% |
| 4 | 27,086 | 43,907 | 16,821 | 38.3% |
| 5 | 45,115 | 61,936 | 16,821 | 27.2% |
| 6 | 45,115 | 61,936 | 16,821 | 27.2% |
| 7 | 46,253 | 79,895 | 33,642 | 42.1% |
| 8 | 50,603 | 101,066 | 50,463 | **49.9%** |

Turns 1–3 show 0%: those turns carry each file block's **first** occurrence,
which the hook must keep (keep-first). Savings appear from turn 4 once
duplicates age past the `protected_tail=2` guard. This is the honest shape of
the method — it saves on *re-reads*, not first reads.

## What this measures (and doesn't)

- **Measures:** input-prompt-token savings and billed cost on a real
  accumulating agent loop; provider prefix-cache behaviour (effective $/M,
  cache-read tokens).
- **Does NOT measure:** task success / pass@1. The `protected_tail=2` design
  keeps the model's last turn intact as the mitigation, but that is a design
  guarantee, not a measured quality result. See "Honesty" below.

## Methodology

- **Model:** `openrouter/deepseek/deepseek-v4-flash` via a LiteLLM proxy.
- **Dataset:** `princeton-nlp/swe-bench_lite` (test split); 10 instances, one
  per repo (astropy, django, matplotlib, seaborn, flask, requests, xarray,
  pylint, pytest, scikit-learn). File content fetched from GitHub raw at
  `base_commit`, 200-line head+tail blocks.
- **Workload:** 8-turn scripted debugging conversation per instance (read
  problem → read sources → re-read → propose fix → re-read → apply → re-read →
  read tests). File blocks returned as `role:"tool"` messages; re-reads are
  byte-identical, which is what the hook targets.
- **Accumulation:** turn N is sent as ONE request carrying turns 1..N — the way
  a real agent sends its context each turn. The hook fires on every request
  individually, stubbing duplicates between the current turn's tool messages
  and the accumulated past.
- **Arms:**
  - ARM A (WITH dedup): proxy with `callbacks: toolrecall.adapters.litellm.handler`.
  - ARM B (WITHOUT dedup): same proxy with `TOOLRECALL_DEDUP_DISABLED=1`.
  - Payloads identical between arms; same conversations, same order, same model.
- **Token metric:** `usage.prompt_tokens` from every `/v1/chat/completions`
  response (provider-billed count).
- **Cost metric:** `usage.cost` from every response; summed.
- **Cache metric:** `usage.prompt_tokens_details.cached_tokens`, summed.

## How the hook works

In agent loops the same file contents are returned as tool messages multiple
times (each re-read produces a byte-identical tool message). The hook scans
`data["messages"]` before forwarding to the provider and replaces every
duplicate after the first with a stub:

```
[toolrecall-dedup] Duplicate content omitted (chars, sha256:abc...).
The byte-identical content already appears in message 4 of this request.
```

Design: **keep-first** (preserves provider prefix caching), deterministic,
fails open, **protected tail** (last 2 messages never stubbed), opt-in.

## Honesty

**Token savings verified; task-quality NOT verified.** The dedup result above
is real, billing-verified token/cost data. What is **not** demonstrated is that
dedup preserves task success (pass@1):

- A SWE-bench **pass@1** A/B on the same tasks was attempted but is
  **unmeasured/inconclusive**: the baseline model (DeepSeek V4 Flash) scores 0
  on the chosen flask tasks even on a correct, isolated environment (gold
  patches pass; the model's contract-mismatched / incomplete fixes don't), so
  there is no nonzero baseline against which a WITH vs WITHOUT dedup effect can
  be measured.
- Do **not** claim "dedup improves or preserves task quality." The defensible
  statement is: **"the hook removes wasted input tokens (billing-verified); its
  effect on task success is unverified."**
- Savings are workload-dependent: an agent that *rewrites whole files* each
  turn (content changes between re-reads) gets less than a re-read-only loop.

## Reproduce

```bash
# Requires: litellm, toolrecall, OPENROUTER_API_KEY
export OPENROUTER_API_KEY=sk-or-...
cd ~/toolrecall && bash bench/litellm_dedup/run_accumulate.sh
# → /tmp/swe_accum_with.json, /tmp/swe_accum_without.json, comparison printed
```

Files:
- `measure_swebench.py` — measurement script (`--accumulate` mode)
- `run_accumulate.sh` — full A/B runner (both arms, 80 req each)
- `litellm_accum_results.json` — raw + aggregated results (WITH / WITHOUT)
- Hook implementation: `toolrecall/adapters/litellm.py`

---

## Customer in-perimeter duplicate-ratio scan (`measure_duplicates.py`)

A zero-trust tool for a prospective buyer to measure **their own** duplicate rate
*before* any pilot — sees exactly what they'd save without giving us (or shipping)
anything. It runs entirely inside their perimeter: reads a slice of their request
bodies, computes what `dedup_messages` could stub, prints a report, sends NOTHING out.

**Why it exists:** our −32.3% is one harness's formatting on one benchmark. A file
that's byte-identical on disk is a *different block* once two harnesses wrap it
differently (line-number prefixes, path headers, truncation markers). Neither we nor
a customer can know their real duplicate rate without measuring it — so this script
answers "what is *your* duplicate ratio?" before anyone commits to a gateway pilot.

**It does NOT need:** ToolRecall the daemon, LiteLLM, a gateway change, an API key, or
approval. It reuses only the pure `dedup_messages()` function (loaded from
`toolrecall/adapters/litellm.py` by path, without importing litellm) and the Python
standard library. It runs against a **JSONL export** — there is no network access.

**Use (from inside their perimeter, on their own traffic):**
```bash
# JSONL of OpenAI chat.completions request bodies, one per line:
python3 bench/litellm_dedup/measure_duplicates.py traffic.jsonl --min-chars 400 600 800
# ...or from stdin:
cat traffic.jsonl | python3 bench/litellm_dedup/measure_duplicates.py -
```

**Reports per `min_chars` threshold:** requests-with-any-dup, total blocks stubbable,
chars (~tokens) stubbable, and % of message volume stubbable.

**Two things the output deliberately does NOT do:**
1. It reports **volume stubbable**, NOT billed-$ savings. Real savings depend on
   prefix-cache-hit economics (cached input runs ~10% of uncached on many rosters), so
   a proper pass/fail must include `cache_read_tokens`, not just prompt tokens.
2. It is a **pre-pilot triage** tool. If a customer's ratio is ~4%, both sides save a
   week; if ~30%, they have an internal number to justify the real pilot.

**Runs at `protect_last=0`** so the scan itself is prefix-stable (the buyer-run pilot
config). The recommendation for any real pilot is `TOOLRECALL_DEDUP_PROTECT_LAST=0` so
cacheable prefixes are never invalidated by a sliding tail.

