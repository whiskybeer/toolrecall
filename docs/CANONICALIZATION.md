# Request Canonicalization (warp profile)

> **Status: experiment, not a feature.** The `warp` canonicalization profile ships
> behind an explicit opt-in header (`X-ToolRecall-Canonicalize: warp`). The ToolRecall
> edge adds this header automatically (TOOLRECALL_EDGE_CANONICALIZE=auto); nothing else
> sets it. See [the Warp benchmark page](https://toolrecall.dev/warp-benchmark) for the
> measured evaluation, including its known gaps.

## What it is

The forward proxy's `api_cache` keys are the SHA-256 of the **entire request body**.
That guarantee — only byte-identical requests share a cache entry — is the safest
possible design, but agent harnesses make byte-identical requests nearly impossible:
every session injects volatile fields (run UUIDs, timestamps, environment snapshots)
that are irrelevant to the model's answer but unique per session.

Canonicalization computes the cache key over a **normalized copy** of the body instead.
Volatile fields are replaced with fixed placeholders before hashing, so two requests
that differ only in noise produce the same key and replay at $0.

**The provider always receives the original, unmodified body.** Canonicalization only
affects the key. Nothing upstream can be poisoned by a normalization mistake.

## What this really is (read this part)

Canonicalization is a **hand-written judgment** that certain differing fields are
semantically irrelevant. That is a different category from the deterministic
argument-normalization in [NORMALIZER.md](NORMALIZER.md):

- Argument normalization maps *the same call, written differently* to one key. The
  semantics are provably identical (key order, whitespace, noise fields).
- The warp profile maps *different requests* to one key, on the assumption that the
  differing fields don't matter. Deterministic, yes — but the assumption is ours.

That makes it the same category of thing we criticize LLM-summarizer caches for,
minus the LLM: a guess, but a fixed, reviewable, evidence-based one. Every rule in
the strip list exists because a live capture showed that exact field forking keys.

**Concretely, the known risks:**

| Stripped field | Collision it creates |
|---|---|
| Skills array → placeholder | Two sessions with 75 vs 74 available tools shared one key (measured 2026-09-04). Same prompt, different tool surface → replay. |
| `directory_state` / env objects removed | Same prompt issued from a different working directory replays the first directory's answer. |
| Context section stripped | Prompt-built context that lived in that section is treated as noise. |

The negative controls in the benchmark (different task, context growth) do **not**
cover the failure mode canonicalization creates: **same prompt, different environment**.
That control has not been run yet, and a Pass@1-delta eval (cached agent vs fresh agent,
same instances, same pass rate) is the gate for promoting this profile from experiment
to default-on.

## The rules (current strip list)

Applied to every string leaf in the message tree, then the envelope is re-serialized
with sorted keys:

1. **CR normalization** — `\r\n` and lone `\r` → `\n`. PowerShell/terminal echo reaches
   Turn-0 with either depending on shell version (measured: a single `\r` forked the key).
2. **Blank-line collapse** — runs of 2+ newlines → one. Clipboard artifacts from pasted
   prompts (`\n\n\n` vs `\n\n\n\n`) are never model-relevant.
3. **UUID placeholder** — v4-shaped UUIDs (run IDs) → `00000000-0000-4000-8000-canonuuidplaceholder`.
4. **Timestamp placeholder** — ISO-8601 UTC timestamps → `1970-01-01T00:00:00Z`.
5. **Conversation-context section removal** — the harness's per-session context block.
6. **Environment object removal** — `directory_state`, `shell`, `operating_system` objects.
7. **Skills-list placeholder** — arrays of `{name,...}` objects (the tool/skill list)
   → a fixed placeholder (they differ per session as skills are added/removed).

Raw-SSE replay pairs with this: on a HIT, the *original* provider stream is replayed
byte-for-byte, so the client sees exactly what a live call delivered.

## Measured evidence

From the live Warp benchmark (2026-09-04/06, see
[warp-benchmark](https://toolrecall.dev/warp-benchmark)):

- Without canonicalization: **0% hit rate** behind Warp — volatile fields fork every key.
- With the profile: replay pairs converge (7 fresh-session Turn-0 bodies → 5 keys;
  the two artifact-caused divergences merged, genuinely different prompts stayed distinct).
- Published sanitized diffs of two verified replay pairs: only UUID + timestamp paths
  diverged; zero content, env, or tool-surface divergence survived in those pairs.

## Enabling

The edge sets the profile per deployment (Warp users change nothing):

```bash
TOOLRECALL_EDGE_CANONICALIZE=auto   # edge adds X-ToolRecall-Canonicalize: warp
```

Custom agents can set the header per request: `X-ToolRecall-Canonicalize: warp`.

## See also

- [NORMALIZER.md](NORMALIZER.md) — tool-call argument normalization (a different,
  provably-safe layer: same call, different formatting).
- [FORWARD_PROXY.md](FORWARD_PROXY.md) — the api_cache and whole-body key hashing.
- Warp benchmark page — measured results, honest limits, and the gating eval.
