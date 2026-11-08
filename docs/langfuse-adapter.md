# Langfuse Adapter — cached tools with verdicts in your Langfuse traces

> No pip extra needed at runtime — langfuse is your application's own
> dependency. The adapter module imports cleanly without langfuse installed;
> only the span-metadata reporting is skipped.

One integration point for [Langfuse](https://langfuse.com): a **`@cached_tool`
decorator** that checks ToolRecall's cache before your tool runs and reports
the verdict into the current Langfuse span's metadata.

---

## Why

Langfuse shows you the problem in the trace: which generations and tool calls
were billed, how often, at what cost. ToolRecall is the layer underneath that
removes the repeats. This adapter composes the two:

- **Same tool + same args → zero re-execution.** Deterministic tool calls
  (search, fetch, parse) are served from cache in ~1ms across runs, threads,
  and experiment arms.
- **The trace shows what was deterministic.** Every hit/miss verdict rides
  into the span metadata under `metadata.toolrecall` — hit, key prefix, age,
  TTL, stale/bypassed flags. In the Langfuse UI you can filter observations
  by `toolrecall.hit = true` and see exactly which tool work was served
  without re-billing.
- **Works with the rest of the Langfuse stack.** The decorator composes with
  `@langfuse.observe()`, with `langfuse.openai` wrapper generations, and with
  dataset experiments (re-run an eval arm and the repeated tool calls come
  back from cache at zero cost — score the prompt, not the tool noise).

---

## Quickstart

```python
from langfuse import observe
from toolrecall.adapters.langfuse import cached_tool

@cached_tool(ttl=300)
def search_web(query: str) -> dict:
    return external_api_call(query)          # runs only on cache miss

@observe                                     # active span → verdicts land here
def agent_turn(question: str) -> str:
    docs = search_web(question)              # 1st call: live + stored
    docs = search_web(question)              # 2nd call: cache hit, verdict in span metadata
    return synthesize(question, docs)
```

Span metadata on the cached observation:

```json
{
  "toolrecall": {
    "hit": true,
    "key": "3f9c2ab1e7d0",
    "age_s": 4.2,
    "ttl": 300
  }
}
```

Miss verdicts are reported too (`"hit": false`) — so the trace shows which
tool observations were billed and which were served deterministically.

## Merging with your own span metadata

Langfuse v4's `update_current_span(metadata=...)` replaces the whole metadata
object — merge before it is written (the adapter does this internally; use
the same pattern when reporting manually):

```python
from langfuse import get_client
from toolrecall.adapters.langfuse import cache_verdict_metadata

meta = {"step": "retrieval", "attempt": 2}
get_client().update_current_span(
    metadata={**meta, **cache_verdict_metadata({"hit": True, "key": "abc"})}
)
```

---

## Semantics

| Concern | Behavior |
|---------|----------|
| Cache key | Bound signature of the wrapped function — `f(1, 2, c=3)` and `f(1, b=2, c=3)` collide into one entry; defaults included |
| Namespace | `langfuse` (via the `mcp_cache` request-key formula) — never collides with other adapters |
| Storable results | Only JSON-round-trip-stable values (`json.loads(json.dumps(v)) == v`). Objects/sets/NaN execute live every time — a stored repr would come back as a *different* value on hit |
| Daemon down | Function executes live, every call, no crash (graceful bypass) |
| Cache errors | Check/store failures log a warning and never break the tool |
| ttl=0 | Full bypass; verdict reports `bypassed: true` |
| Stale (SWR) | Stale-but-within-window hits are served with `stale: true` in the verdict; revalidation is the caller's job (see [MCP_CACHE_POLICY](MCP_CACHE_POLICY.md)) |
| Async tools | `async def` gets an async wrapper automatically |
| Without langfuse | Caching works; verdict reporting silently skips (debug-log note) |

---

## What this adapter is NOT

- **Not an LLM-call cache.** `langfuse.openai` generations keep flowing to
  your provider; this caches *tool* results around them. For LLM-request
  dedup at the gateway, see the LiteLLM adapter.
- **Not a Langfuse plugin.** The adapter never touches Langfuse's ingestion
  path — it only writes span metadata through the public SDK API.

---

## Verifying the savings in Langfuse

1. Wrap your deterministic tools with `@cached_tool`.
2. Run the same agent session (or dataset experiment) twice.
3. In the Langfuse UI, filter observations where `toolrecall.hit` is true —
   those tool calls re-executed at zero cost. Compare billed cost of run 2
   vs run 1 for the equal-work delta.

> Honest accounting note: quote **billed cost per run** (per-arm), never
> cumulative "context dropped" — the Langfuse ledger is exactly the right
> place to verify this, which is the point of the integration.