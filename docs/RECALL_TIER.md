# Recall Tier — lossless-recoverable eviction for non-reproducible content

> **Opt-in, default OFF.** Enables with `[recall].enabled = true` (or
> `TOOLRECALL_RECALL_ENABLED=true`). Adds **zero runtime dependencies** and
> does not change the deterministic cache behavior until you turn it on.

## Why it exists

ToolRecall's Context Tracker can drop a *file* from the agent's context
because re-reading it from cache is **byte-identical** — the drop is free
and lossless. That guarantee holds only for content with a deterministic
re-fetch key: a path, a command hash, a request hash.

For **non-reproducible** content — a one-shot API result, a live web
snapshot, ephemeral tool output that would never come back identical —
re-fetching yields different bytes. There is no cache key to re-read, so
today that content *must* stay in the live context at full token cost
every turn.

The Recall Tier closes exactly this gap.

## The contract

```
1. recall_store(fingerprint, content, content_type, reproducible=False)
     → persists the raw content out-of-band
     → returns a tiny deterministic node_id pointer
2. Agent drops the verbose raw block from context, keeps only the pointer
3. recall_get(node_id) → restores the raw bytes on demand
```

- **node_id is deterministic** — derived from the fingerprint (`_hash`), not
  an RNG. Re-storing the same fingerprint dedups to the same row.
- **Raw is never destructively compressed** — it is persisted verbatim to
  the recall cache and restored exactly. This is a *recoverable eviction*,
  not a lossy summary.
- **Reproducible content is untouched.** The `reproducible(content_type, key)`
  classifier sends the file/terminal/api/mcp/browser keyed kinds back through
  the existing byte-identical cache; only the non-reproducible tail reaches
  the recall tier.

## Positioning vs. the Context Tracker

| | Context Tracker | Recall Tier |
|---|---|---|
| Content | files (deterministic re-fetch) | web / API / ephemeral (non-deterministic) |
| Eviction | drop clean file content | drop verbose raw, keep `node_id` pointer |
| Restore | re-read from file cache (byte-identical) | `recall_get(node_id)` (byte-identical) |
| Loss | none | none (raw persisted, not summarized) |

Both are *eviction contracts*; they differ in *what* they can safely evict.

## Interfaces

Three ways to drive the tier — all talk to the same daemon:

- **Daemon**: `recall_store`, `recall_get`, `recall_stats`.
- **MCP tools** (listed only when `[recall].enabled`): `recall_store`,
  `recall_get`.
- **CLI**: `toolrecall context recall store <fingerprint> [content_type]`
  (content from stdin), `toolrecall context recall get <node_id>`,
  `toolrecall context recall status`.

## Accounting

Every `recall_get` hit records tokens into a dedicated `recall` category in
`cache_stats`:

- `hits` incremented
- `tokens_saved` / `context_tokens_saved` += the entry's token estimate

`recall_stats` returns the aggregate (`total` entries, persisted `tokens`).
Because recall savings are attributed to their own sink — never merged into
the file-cache counters — the tier's contribution is measurable **and never
double-counted** against normal cache savings.

## Zero runtime dependencies

`recall.py` uses only `toolrecall._db` + stdlib. The `summary` column exists
but is empty unless the caller supplies one. LLM summarization is a future
optional pip-extra (`toolrecall[recall]`) — not a core dependency.

## Design notes / open items

- **Explicit-agent-call mode is v1.** The agent calls `recall_store`/`get`
  directly. Automatic compaction of non-reproducible blocks on eviction
  (a bridge hook) is a later product decision, not implemented here — it
  trades transparency for automation and is deliberately out of scope.
- Empty content is rejected (nothing useful to persist).
- The MCP tools are gated on `[recall].enabled`, mirroring the
  terminal/invalidate gate pattern.