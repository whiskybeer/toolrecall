# Request Canonicalization (warp, hermes, claude-code profiles)

> **Status: experiment — controls passed 2026-09-08, still profile-scoped and opt-in.**
> The `warp` canonicalization profile ships behind an explicit opt-in header
> (`X-ToolRecall-Canonicalize: warp`). The ToolRecall edge adds this header automatically
> (TOOLRECALL_EDGE_CANONICALIZE=auto); nothing else sets it. Both gating controls
> (Pass@1 delta, same-prompt/different-environment) ran on 2026-09-08 and passed —
> see [Controls run](#controls-run) below. [The Warp benchmark page](https://toolrecall.dev/warp-benchmark)
> carries the full measured evaluation.

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

## Controls run (2026-09-08) — both passed

Both gating controls ran on 2026-09-08 through the live Warp harness (SWE-bench
astropy #12907 + django #10914, full data in the
[gate-run results](https://toolrecall.dev/bench-data/warp/gate-eval-2026-09-08.md)):

**1. Pass@1 delta (astropy, two arms × two passes):**

| Arm | Pass | Replay HITs | Outcome |
|---|---|---|---|
| C1 (fresh sessions, std cwd) | populate → replay | 10 HITs in 16 s (435,946 pt, $0) | same root cause, same fix, 11/11 tests |
| C2 (fresh sessions, env2 cwd) | populate → replay | 4 HITs (153,153 pt, $0) | same root cause, same fix, 11/11 tests |

Pass@1 delta: **0**. Cached-replay sessions reported the identical root cause
(`_cstack`, nested-right branch) and applied the identical fix as fresh sessions.

**2. Same-prompt/different-environment (django, the named collision risk):**

Populate ran from the standard working directory; the control session launched from
a second checkout (`django-env2`) with a **byte-identical prompt text**. Result: the
env2 turn-0 produced a **different canon key** (`c83218a5…` vs `3ab5adcd…`/`5f6232ed…`),
was served live, never cached, never replayed. **No false sharing** — Warp's
orchestration-runtime context survives canonicalization and forks the key as designed.

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

## `claude-code` profile

Opt-in via `X-ToolRecall-Canonicalize: claude-code` (Claude Code sends it with
`ANTHROPIC_CUSTOM_HEADERS`). Built from a raw-body diff of identical headless
`claude -p` runs; each rule removes one measured source of key divergence:

| Rule | Why |
|---|---|
| `hermes` text rules (CR, blank lines, UUID, ISO timestamp) | shared harness noise |
| top-level `metadata` dropped from the key | carries account + per-run session id; never shown to the model |
| `in/took <n>(ms\|s\|sec\|seconds)` → `in <duration>` | test-runner wall time (`2 passed in 0.25s`) was the first divergence after a replayed call |

Deliberately **not** normalized: tool descriptions (a server-side flag flips the
Bash tool text in ~1 of 4 runs — real model-visible text, so it forks the key),
and `ls -l` mtimes (too broad to strip safely). Both fork the replay chain at that
point; the forked branch is cached and replays on later runs.

Measured (n=8 repeats of one toy task, Haiku): 16/24 calls replayed, 4/8 runs fully
replayed, −62% usage vs the cold run; the `hermes` profile reached 6/24 and −24%.
On 2 real SWE-bench Lite tasks (Sonnet, 5 runs each): warm runs −69% when tests
are run, −100% when not; on a 5-issue session (33–38 calls): −18%. Remaining breakers: pytest's numbered temp dirs (a
candidate rule, not added to avoid fitting the bench) and `grep -r` output order.
Details: [bench/claude_code_replay/RESULTS.md](../bench/claude_code_replay/RESULTS.md).

**Controls (2026-10-22) — passed.** 22 runs on two SWE-bench Lite django tasks:
same prompt from another checkout dir → 0 hits; source edit visible in `git status`
→ 0 hits; the same edit hidden from `git status` → replay stopped at the first tool
output containing it; other model → 0 hits; Pass@1 6/6 fresh vs 8/8 replay arm.
An audit of all 79 replayed calls across every experiment found only intended
differences (test durations), 0 false matches. Known collision risks (different
accounts share entries because `metadata` is dropped; timing-sensitive work;
semantic UUIDs/timestamps) and details: [RESULTS.md](../bench/claude_code_replay/RESULTS.md).

Same caveat as every profile: replay serves the **earlier answer** — use it for
CI re-runs and regression runs, never for evals that sample the model.

## See also

- [NORMALIZER.md](NORMALIZER.md) — tool-call argument normalization (a different,
  provably-safe layer: same call, different formatting).
- [FORWARD_PROXY.md](FORWARD_PROXY.md) — the api_cache and whole-body key hashing.
- Warp benchmark page — measured results, honest limits, and the gating eval.
