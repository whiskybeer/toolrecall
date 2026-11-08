# LangGraph Adapter — checkpoints + tool cache for LangGraph agents

> `pip install toolrecall[langgraph]` — adds `langgraph>=1.0` as the only extra
> dependency. The adapter module itself imports cleanly without langgraph.

Two integration points for [LangGraph](https://langchain-ai.github.io/langgraph/):
a **checkpoint saver** (graph state) and a **tool decorator** (node tool calls).
Both are thin wrappers over ToolRecall's cache — no daemon changes, no
framework monkey-patching.

---

## Why

LangGraph already has checkpointing and time travel — but every fork/resume
re-bills the full prompt and re-executes every tool on the resumed path.
ToolRecall adds the deterministic-execution layer underneath:

- **Checkpoints survive restarts** (SQLite-backed via the ToolRecall daemon,
  per-thread/per-namespace keys) instead of living in a memory saver.
- **Resuming from `checkpoint_id` restores the exact node** — the fork/resume
  primitive tree-search and mutation-loop orchestrators branch from.
- **Repeated node work is cached**: same tool + same args → cached result,
  zero re-execution across threads and across branch experiments.

---

## Quickstart

```python
from langgraph.graph import StateGraph, END
from toolrecall.adapters.langgraph import get_saver, cached_tool

@cached_tool(ttl=300)                      # node tool calls, cached
def search_web(query: str) -> str:
    return external_api_call(query)

builder = StateGraph(MyState)
builder.add_node("search", search_node)
builder.set_entry_point("search")
builder.add_edge("search", END)

graph = builder.compile(checkpointer=get_saver(ttl=86400))

cfg = {"configurable": {"thread_id": "run-1"}}
graph.invoke(inputs, config=cfg)
```

## Fork / resume (time travel)

```python
hist = list(graph.get_state_history(cfg))            # oldest → newest
target = hist[2].config["configurable"]["checkpoint_id"]

# Resume the graph from checkpoint #2 — state is restored from
# ToolRecall's cache; subsequent tool calls only execute for
# work not already cached.
graph.invoke(None, config={**cfg, "configurable": {"checkpoint_id": target}})
```

Branching pattern for mutation loops: run the graph to checkpoint N, then
launch B fresh threads each starting from
`{**cfg, "configurable": {"checkpoint_id": <N>}}` with a mutated node
implementation. Shared-prefix tool calls are served from cache at $0; only the
divergent steps execute.

## What is stored

| LangGraph concept | ToolRecall storage |
|---|---|
| Checkpoint (channel values, versions) | `mcp_cache` row keyed `langgraph://checkpoint?key=lg:<thread>:<ns>:<ckpt_id>` |
| Checkpoint metadata + parent link | Same row (`parent` → parent config) |
| Pending writes (interrupts, task writes) | Same row, serde-serialized per write |
| Thread index (ordering, latest) | `langgraph://index` rows per thread/namespace |
| Node tool results | `langgraph://<tool_name>?key=<args-hash>` |

Thread state survives daemon restarts (persistent SQLite). History ordering
follows write order; `list()` returns oldest → newest.

## Graceful degradation

- Daemon down → `cached_tool` executes live; saver writes are no-ops, reads
  return `None` (LangGraph then treats the thread as empty). Nothing raises.
- Unknown `checkpoint_id` → `get_tuple` returns `None` (LangGraph's standard
  "no such checkpoint" path).

## Limitations (v1)

- **Default serde is JSON-only.** Channel values that aren't JSON-serializable
  (arbitrary objects) need a custom serde:
  `get_saver(serde=MyJsonPlusSerializer())`. Base64/pickle support is a
  possible follow-up.
- **`delete_thread` is logical** — the thread index is cleared (so latest/list
  reads see nothing), but row data ages out via TTL; `mcp_cache` has no
  per-key delete in v1.
- **Sync-path only** for the daemon round-trip (async saver methods delegate
  to the sync ones — SQLite via the daemon is sync anyway).
- No per-branch namespace isolation yet: two agents sharing one thread_id on
  one daemon share checkpoint rows. Give each branch its own `thread_id`
  (the intended pattern) until per-client scoping lands.

## Related

- Design note for the tree-search use case this enables: `docs/TREE_SEARCH.md` (cost model, phased contract, GEPA integration point; the first billed fork A/B is linked from its status banner)
- LangChain adapter: `docs/langchain.md` (LLM cache + callback handler)
- ADK adapter: `docs/google-adk.md` (same `@cached_tool` contract)
