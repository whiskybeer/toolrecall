# ToolRecall Framework Adapters

> Drop-in caching for agent frameworks — without modifying the framework itself.

ToolRecall adapters bridge ToolRecall's SQLite-backed cache into popular agent development frameworks. Each adapter is a thin wrapper around `toolrecall.cache`, talks to the daemon over UDS, and handles framework-specific types and lifecycle transparently.

---

## Available Adapters

| Adapter | Tool | LLM | Setup |\n|---------|------|-----|-------|\n| **Google ADK** | ✅ `@cached_tool` decorator | ✅ Forward proxy (auto) | `pip install toolrecall` |\n| **LangChain / LangGraph** | ✅ `ToolRecallCallbackHandler` | ✅ `ToolRecallCache` BaseCache | `pip install toolrecall[langchain]` |\n| **herdr** | ✅ `tr` binary + MCP bridge | — (shell-level) | Build `tr`, `toolrecall mcp` |\n| **Odysseus** | ✅ `install_agent_cache()` + `install_mcp_cache()` | ✅ Forward proxy (auto) | `pip install toolrecall` |

---

## Google ADK — `@cached_tool` Decorator

```python
from toolrecall.adapters import google_adk
from google.adk.tools.function_tool import FunctionTool

@FunctionTool
@google_adk.cached_tool(ttl=300)
def search_web(query: str) -> str:
    return external_api_call(query)  # Only on cache miss
```

The decorator wraps any `@tool` function. On repeat calls with identical args, the cached result returns in ~0.6ms — zero API calls, zero tokens.

- **Async-safe:** `async def` tools get async wrappers automatically
- **No framework monkey-patching:** ~30 lines, works with any ADK version
- **Namespace:** `adk` — keys don't collide with other adapters
- **Graceful bypass:** Daemon not running → function executes live, no crash

[Full docs →](../../docs/google-adk.md)

---

## LangChain / LangGraph — `ToolRecallCache` + Callback

Two integration points:

### LLM Cache (BaseCache subclass)

```python
from langchain.globals import set_llm_cache
from toolrecall.adapters.langchain import ToolRecallCache

set_llm_cache(ToolRecallCache())

# Every LLM call now checks ToolRecall's local SQLite first.
# Cache hit → returns instantly. Cache miss → calls LLM, stores result.
```

- Compatible with any LangChain LLM provider (OpenAI, Anthropic, Google, local…)
- Persistent SQLite — survives restarts
- TTL-based expiration (daemon default or custom)

---

## Odysseus — `install_agent_cache()` + `install_mcp_cache()`

Agent tool and MCP server caching for the [Odysseus AI workspace](https://github.com/pewdiepie-archdaemon/odysseus).

Two integration points:

### Agent Tool Cache

```python
from toolrecall.adapters.odysseus import install_agent_cache

# Wrap tool_execution.py with transparent caching
install_agent_cache()
```

Every tool block execution (shell, script, search, web_fetch, etc.) is cached by
tool name + arguments hash. Repeat calls serve from cache — zero re-execution.

### MCP Server Cache

```python
from toolrecall.adapters.odysseus import install_mcp_cache

# Wrap McpManager for cached MCP server results
from src.mcp_manager import McpManager
mgr = McpManager()
install_mcp_cache(mgr)
```

Built-in MCP servers (email, memory, rag, image_gen) return cached results for
repeat calls with identical arguments.

- **Graceful bypass:** ToolRecall daemon not running → all calls pass through
- **Namespace:** `odysseus` — isolated from other adapters
- **Async-safe:** `@cached_async_tool` decorator for async tool functions
- **No new dependencies:** uses `toolrecall.cache` — already installed

[Full docs →](../../docs/odysseus.md)

### Tool Cache (Callback Handler)

```python
from langchain.callbacks.base import BaseCallbackManager
from toolrecall.adapters.langchain import ToolRecallCallbackHandler

callback = ToolRecallCallbackHandler()
manager = BaseCallbackManager.add_handler(callback)

# Tool results cached under tool name + args hash.
# Same tool + same args → cached result, no re-execution.
```

- Intercepts `on_tool_end` → stores result keyed by tool name + input
- Error results are not cached
- Best-effort: failures log a warning, never raise

### Lazy Base Binding

The module is importable **without** `langchain_core` installed. Base class inheritance (`BaseCache`, `BaseCallbackHandler`) is resolved lazily on first use via `_ensure_base()`. If LangChain is absent, `ToolRecallCache()` raises a clear `ImportError` with install instructions.

**Namespace:** `langchain`

[Full docs →](../../docs/langchain.md)

---

## herdr — Terminal Multiplexer Integration

ToolRecall works with all 21 agents herdr supports. Two paths:

```
# Path 1: tr binary (universal — any agent, any pane)
tr read path/to/file       # Cached file read
tr term "hostname"          # Cached terminal command

# Path 2: MCP bridge (for MCP-capable agents)
toolrecall mcp              # Exposes cached tools via MCP
```

- **tr binary:** Go client, build once, put on `$PATH`. Every agent pane inherits it.
- **MCP bridge:** `toolrecall setup` writes config automatically for Hermes, OpenCode, etc.
- **Shared cache:** What one pane caches, another can hit.

[Full docs →](../../docs/herdr.md)

---

## Architecture

```
┌──────────────────────────────────────────────────┐
│  Your Agent / Framework                           │
│  ┌──────────┐  ┌──────────┐  ┌───────────────┐  │
│  │  ADK     │  │ LangChain │  │  herdr pane   │  │
│  │ @cached  │  │ Cache+CB  │  │  tr / MCP     │  │
│  └────┬─────┘  └────┬─────┘  └───────┬───────┘  │
│       │              │                │           │
└───────┼──────────────┼────────────────┼───────────┘
        │              │                │
        ▼              ▼                ▼
┌──────────────────────────────────────────────┐
│  ToolRecall Daemon (UDS)                      │
│  ┌────────────┐  ┌────────────────────────┐  │
│  │ Cache core │  │  SQLite (single conn)  │  │
│  └────────────┘  └────────────────────────┘  │
└──────────────────────────────────────────────┘
```

All adapters communicate with the daemon over Unix Domain Sockets. The daemon manages the single SQLite connection — adapters never open a direct DB handle, eliminating lock contention.

---

## Dev Setup

```bash
cd toolrecall
make setup  # Installs all deps

# Test all adapters
make test
# or just the adapter module
pytest tests/test_adapters.py -v
```