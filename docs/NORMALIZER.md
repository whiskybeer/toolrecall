# Cache Key Normalization

> **Introduced in v0.9.0** — Opt-in feature. Enable via `[norm].enabled = true` in config or `TOOLRECALL_NORM_ENABLED=true` env var.

## What It Does

ToolRecall normalizes tool call arguments **before** computing the cache key. This means semantically identical calls produce the same cache key — even when the agent rephrases, reorders, or adds noise to the arguments.

**Three normalization rules:**

| Rule | Before | After | Effect |
|------|--------|-------|--------|
| **JSON key sorting** | `{"b":2,"a":1}` | `{"a":1,"b":2}` | Key order doesn't matter |
| **Whitespace stripping** | `"  /tmp/file  "` | `"/tmp/file"` | Leading/trailing spaces ignored |
| **Noise key removal** | `{"path":"/tmp","session_id":"abc","timestamp":"..."}` | `{"path":"/tmp"}` | Non-semantic fields stripped |
| **Command normalization** | `"  LS   -la  "` | `"ls -la"` | Command name lowercased, whitespace collapsed |

**Noise keys stripped automatically:** `timestamp`, `request_id`, `session_id`, `nonce`, `trace_id`, `span_id`, `correlation_id`, `_t`, `_r`

## Why It Matters

Without normalization, these two MCP tool calls would produce different cache keys:

```python
# Call 1 — agent adds a timestamp
{"owner": "whiskybeer", "repo": "toolrecall", "timestamp": "2026-07-09T12:00:00"}

# Call 2 — agent sends keys in different order with a session ID
{"repo": "toolrecall", "owner": "whiskybeer", "session_id": "sess-abc"}
```

With normalization, both produce the exact same cache key: `{"owner":"whiskybeer","repo":"toolrecall"}`

This means: **the second call hits the cache**, saving a full tool execution round-trip.

## What It Affects

| Cache Type | Normalization Applied |
|------------|----------------------|
| **MCP cache** | ✅ JSON key sorting + whitespace stripping + noise key removal |
| **Terminal cache** | ✅ Command name lowercased, whitespace collapsed |
| **Script cache** | ✅ Same as terminal (path + args) |
| **File cache** | ❌ Uses mtime — no key normalization needed |

## When to Enable

**Enable if:** Your agent frequently rephrases tool calls, sends timestamps, or uses different argument ordering between calls. This is common with:

- Multi-turn agent sessions where the same tool is called repeatedly
- Agents that pass session IDs, trace IDs, or request IDs in every call
- MCP tools like `github`, `fetch`, or custom servers where parameters vary across invocations

**Disable if:** You want to preserve existing cache entries and don't see argument variation between calls.

## Why It's Opt-In (Default: Off)

Normalization changes the cache key computation. Once enabled:

1. **New cache keys differ** from old ones — existing entries become orphans
2. **Orphans are harmless** — old entries remain in the database but are never matched. They consume space until the database is pruned or the cache is cleared.
3. **No data loss** — the old cache data is still there if you disable normalization again

## Enabling

```toml
# ~/.config/toolrecall/toolrecall.toml
[norm]
enabled = true
```

Or via environment variable:

```bash
export TOOLRECALL_NORM_ENABLED=true
```

Or during `toolrecall init` — the interactive setup prompts you with a detailed explanation and asks for confirmation.

## What It Does NOT Do

- **No semantic embeddings** — this is pure deterministic normalization (sorting, stripping, lowercasing). It does not use any ML model.
- **No false positives** — because it's deterministic, two different arguments never collide. The same input always produces the same key, and different inputs always produce different keys.
- **No data modification** — normalization only affects the cache key. The actual cached response is stored and returned as-is.

## Canonical Commands (`[norm].canonical_commands`)

`canonical_command()` extends `normalize_command()` with deeper syntactic
canonicalization for terminal cache keys:

| Transformation | Example |
|----------------|---------|
| Flag-cluster sorting with combined-short-flag decomposition | `ls -al` ≡ `ls -la` ≡ `ls -a -l` |
| `~` expansion | `cat ~/notes/x.md` ≡ `cat /home/user/notes/x.md` |
| Duplicate-slash and trailing-slash collapse | `ls /tmp//` ≡ `ls /tmp` |
| Redundant quote removal | `echo "hi"` ≡ `echo hi` |

Conservative by design: only contiguous flag clusters sort (assumed
order-independent); long flags (`--oneline`) and valued flags (`-n 5`) stay
whole; shell metacharacters abort canonicalization (compound commands are
never cacheable anyway); `shlex` failures fall back to the base
normalization — never raise.

**Serving stays exact-hash-key.** Canonicalization only widens which
invocations produce the *same* key; entries are never served across
different keys, so cross-key poisoning is impossible. Note: enabling this
after running with it off invalidates existing terminal entries once
(cache keys change — one re-warm).

## Fuzzy TTL Classification (`[cache].fuzzy_ttl_match`)

Complementary, also deterministic (difflib): when a terminal command
matches **no** exact/prefix pattern, the most-similar known pattern at or
above `fuzzy_threshold` (default 0.85) lends its TTL/cacheability. This is
**classification only** — it decides *whether* and *how long* to cache; it
never serves one command's entry under another command's key.

## Future: Semantic Fallback

The current normalization is **syntactic** — it handles reordering, whitespace, and noise. A future version may add **semantic matching** using a local embedding model to catch paraphrases ("Fetch stats for July" → "Retrieve data for 07/2026"). This would be a separate optional feature with its own config toggle.