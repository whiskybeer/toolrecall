# ToolRecall + LangChain / LangGraph

> **Date:** July 2026 · `langchain-core` v0.3+
> **Principle:** ToolRecall extends LangChain's existing cache abstraction — no framework changes needed.

## Table of Contents

- [Overview](#overview)
- [Installation](#installation)
- [LLM Cache (BaseCache)](#llm-cache-basecache)
- [Tool Cache (cached_tool)](#tool-cache-cached_tool)
- [LangGraph Support](#langgraph-support)
- [Architecture](#architecture)
- [Comparison: TR vs LangChain's Built-in Caches](#comparison-tr-vs-langchains-built-in-caches)
- [Limitations](#limitations)

---

## Overview

ToolRecall provides two integration points for LangChain:

| Integration | What it caches | Setup |
|------------|---------------|-------|
| **`ToolRecallCache`** (BaseCache) | LLM responses — any provider (OpenAI, Anthropic, local) | `set_llm_cache(ToolRecallCache())` |
| **`cached_tool(tool)`** | Tool execution results, keyed by tool name + input | `tool = cached_tool(tool)` |

Both work with LangGraph's state-based execution. The cache is persistent SQLite — survives process restarts and is shared across all agents on the machine.

---

## Installation

```bash
pip install toolrecall
pip install langchain langchain-core
# The adapter imports langchain_core lazily at runtime (there is no
# toolrecall[langchain] extra)
```

The adapter module is importable without LangChain installed — it only raises `ImportError` when you actually try to use it. This means `pip install toolrecall` (base) doesn't pull in LangChain.

---

## LLM Cache (BaseCache)

ToolRecall implements LangChain's `BaseCache` interface, so any `set_llm_cache()` call works:

```python
from langchain.globals import set_llm_cache
from toolrecall.adapters.langchain import ToolRecallCache

# One line: every LLM call now checks ToolRecall's SQLite cache first
set_llm_cache(ToolRecallCache())

# Cache hit:  returns instantly, zero tokens consumed
# Cache miss: calls the LLM, stores the result
```

### How it works

- **Key:** `llm:{prompt}:{model_identifier}` — collisions only on identical prompts to the same model
- **Storage:** Namespaced under `"langchain"` server in ToolRecall's MCP cache
- **TTL:** Configurable via `ToolRecallCache(ttl=3600)`. Default: daemon's MCP default (~3600s)
- **Lifecycle:** Survives process restarts — SQLite is persistent

### Full example with any provider

```python
from langchain.globals import set_llm_cache
from langchain_openai import ChatOpenAI
from langchain.schema import HumanMessage
from toolrecall.adapters.langchain import ToolRecallCache

# Enable ToolRecall caching
set_llm_cache(ToolRecallCache())

# First call: cache miss, contacts OpenAI
llm = ChatOpenAI(model="gpt-4")
result1 = llm.invoke([HumanMessage(content="What is the capital of France?")])

# Second call: cache HIT, returns instantly from SQLite
result2 = llm.invoke([HumanMessage(content="What is the capital of France?")])
# result2 is identical to result1, but sub-ms latency and zero API cost
```

---

## Tool Cache (cached_tool)

A LangChain callback handler cannot skip tool execution, so tool caching wraps
the tool itself. `cached_tool()` takes any `BaseTool` (for example one made
with `@tool` or `StructuredTool.from_function`) and returns a wrapped copy:

```python
from langchain_core.tools import tool
from toolrecall.adapters.langchain import cached_tool

@tool
def search(query: str) -> str:
    """Search the docs."""
    return run_search(query)

search = cached_tool(search, ttl=300)

search.invoke({"query": "ttl"})  # miss: runs the tool, stores the result
search.invoke({"query": "ttl"})  # hit: served from ToolRecall, tool body skipped
```

### What gets cached

- **Key:** tool name + parsed tool **input** — `{"input": "<str>"}` for string
  input, `{"kwargs": {...}}` for dict/ToolCall args (key order does not
  matter). The output never takes part in the key. Injected arguments
  (`run_manager`, `callbacks`, the `RunnableConfig` parameter, `InjectedToolArg` parameters) are excluded.
  A string input and the equivalent dict input are separate entries.
- **Where it hooks:** the tool's own `_run` / `_arun`. Input validation,
  `on_tool_start` / `on_tool_end` callbacks and `ToolMessage` formatting still
  run on a hit, so `ToolNode` and `AgentExecutor` work unchanged.
- **Values:** stored as JSON. Results that are not JSON-serializable run live
  every time. Tuples come back as lists, except the `(content, artifact)` pair
  of `response_format="content_and_artifact"` tools, which is restored.
- **TTL:** `cached_tool(tool, ttl=300)`. Default: daemon MCP default; `ttl=0`
  bypasses the cache.
- **Errors are NOT cached** — an exception propagates and nothing is stored.
- **Best-effort:** daemon down or any cache error → the tool runs live.
- **Async:** `ainvoke` / `arun` are cached too, sharing entries with the sync path.

### `ToolRecallCallbackHandler` (store-only)

`ToolRecallCallbackHandler` records successful string tool results under the
same input key `cached_tool` reads. It never saves a tool call by itself —
the tool always runs — but a later `cached_tool`-wrapped call with the same
input is served from what it recorded. Non-string outputs (e.g. the
`ToolMessage` returned when a tool is called with a `tool_call_id`) are not
recorded. Prefer `cached_tool` unless you only want to warm the cache.

### Combined usage

```python
from langchain.globals import set_llm_cache
from langchain.agents import AgentExecutor, create_react_agent
from toolrecall.adapters.langchain import ToolRecallCache, cached_tool

# Cache LLM responses
set_llm_cache(ToolRecallCache())

# Cache tool execution results
tools = [cached_tool(t) for t in tools]

# Use in your agent
agent = create_react_agent(llm, tools, prompt)
agent_executor = AgentExecutor(agent=agent, tools=tools)
```

---

## LangGraph Support

ToolRecall works with LangGraph's state-based execution out of the box:

| LangGraph Feature | Works? | How |
|:---|:---:|:---|
| **State checkpointing** | ✅ | ToolRecallCache persists across graph checkpoints — LLM calls are cached regardless of state |
| **Conditional edges** | ✅ | Tool result caching works via `cached_tool` — same tool + same input = cached |
| **Parallel node execution** | ✅ | All nodes share the same daemon — cache hits from one node benefit others |
| **Subgraphs** | ✅ | Parent and child graphs share the same SQLite cache |

No LangGraph-specific code changes needed. `ToolRecallCache` and `cached_tool`-wrapped tools (e.g. in a `ToolNode`) work within any LangGraph node.

### Manual invalidation in a graph

```python
from toolrecall.adapters.langchain import ToolRecallCache

cache = ToolRecallCache()

# In a graph node, clear the LLM cache when state changes significantly:
def process_results(state):
    if state.get("reset_cache"):
        cache.clear()
    # ... rest of the node logic
```

---

## Architecture

```mermaid
flowchart TB
    subgraph Agent["LangChain / LangGraph Agent"]
        LC["ToolRecallCache<br/>(BaseCache)<br/>set_llm_cache(tr)"]
        LH["cached_tool(tool)<br/>(BaseTool wrapper)"]
    end

    subgraph TR["ToolRecall Daemon (UDS)"]
        MCP["MCP Cache (LRU)"]
        SQL["SQLite WAL<br/>Namespace: langchain"]
        DISK["Disk cache.db<br/>(persistent)"]
        MCP --> SQL --> DISK
    end

    LC --> MCP
    LH --> MCP
```

Both adapter components talk to the same daemon over the same UDS socket. The daemon manages the SQLite connection — the adapter never opens a direct DB connection, avoiding lock contention. The namespace `"langchain"` separates LangChain cache entries from entries created by other frameworks (ADK) or the shim.

---

## Comparison: TR vs LangChain's Built-in Caches

LangChain ships with several cache backends. Here's how ToolRecall compares:

| Feature | ToolRecallCache | LangChain SQLiteCache | LangChain RedisCache | LangChain InMemoryCache |
|---------|:---------------:|:--------------------:|:--------------------:|:---------------------:|
| **Persistence** | ✅ SQLite WAL | ✅ SQLite | ✅ Redis | ❌ In-memory only |
| **Shared across agents** | ✅ Same daemon, same SQLite | ❌ Per-process | ✅ (if same Redis) | ❌ |
| **Setup complexity** | ⭐ 1 line | ⭐⭐ Schema management | ⭐⭐⭐ Redis infra | ⭐ None |
| **TTL** | ✅ Configurable | ❌ No TTL | ✅ via Redis TTL | ❌ No TTL |
| **Lock contention** | ✅ Daemon-managed | ⚠️ SQLite locking | ✅ Redis handles it | ✅ None |
| **Extra deps** | ❌ Zero (stdlib) | ❌ Zero | ✅ redis-py | ❌ Zero |
| **MCP awareness** | ✅ Can cache MCP tool results | ❌ | ❌ | ❌ |
| **Namespace isolation** | ✅ Per-framework (langchain/adk) | ❌ Single table | ✅ via Redis key prefix | ❌ |

ToolRecall's edge: zero extra dependencies, daemon-managed concurrency, MCP-aware caching, and persistence across process and agent boundaries.

---

## Limitations

1. **Requires running daemon** — ToolRecallCache returns `None` (cache miss) when the daemon isn't reachable. It never blocks or crashes your LangChain agent.
2. **No streaming cache** — LLM streaming responses are not cached. Only `invoke()` (complete response) goes through BaseCache.
3. **Serialization overhead** — Cache hits involve JSON deserialization + ChatGeneration reconstruction. Overhead is ~0.5ms vs a direct in-memory cache, but negligible compared to LLM latency (~1-30s).
4. **Prompt-level granularity** — The cache key is the full prompt string. Minor whitespace differences produce cache misses. Use the ToolRecall normalizer (`[norm].enabled = true`) for deterministic key generation that strips formatting noise.
5. **LangSmith tracing** — Cached LLM responses skip LangSmith's run tracking (they never reach the LLM). Tool calls served by `cached_tool` are still traced: the wrapper only replaces the tool's `_run`/`_arun`, so tool start/end callbacks fire as usual.