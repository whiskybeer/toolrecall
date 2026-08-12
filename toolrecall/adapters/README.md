# ToolRecall Framework Adapters

> Drop-in caching for agent frameworks — without modifying the framework itself.

ToolRecall adapters bridge ToolRecall's SQLite-backed cache into popular agent development frameworks. Each adapter is a thin wrapper around `toolrecall.cache`, talks to the daemon over UDS, and handles framework-specific types and lifecycle transparently.

---

## Available Adapters

| Adapter | Tool | LLM | Setup |\n|---------|------|-----|-------|\n| **Google ADK** | ✅ `@cached_tool` decorator | ✅ Forward proxy (auto) | `pip install toolrecall` |\n| **LangChain / LangGraph** | ✅ `ToolRecallCallbackHandler` | ✅ `ToolRecallCache` BaseCache | `pip install toolrecall[langchain]` |\n| **herdr** | ✅ `tr` binary + MCP bridge | — (shell-level) | Build `tr`, `toolrecall mcp` |\n| **Odysseus** | ✅ `install_agent_cache()` + `install_mcp_cache()` | ✅ Forward proxy (auto) | `pip install toolrecall` |\n| **LiteLLM Proxy** | ✅ `async_pre_call_hook` dedup | — (gateway-level) | `pip install toolrecall` (+ LiteLLM) |

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

## LiteLLM Proxy — Pre-Call Content Dedup

```python
# proxy_config.yaml
litellm_settings:
  callbacks: toolrecall.adapters.litellm.handler
```

Deduplicates repeated large content blocks within a single chat completion request.
Typically hits when agent loops re-send identical tool output or file contents every
turn. Keep-first strategy preserves provider prefix caching.

- **~280 lines**, no deps beyond toolrecall + litellm
- **Keep-first**: earlier messages never rewritten — provider prefix caching keeps hitting
- **Deterministic**: same messages + same config = same output
- **Protected tail**: last N messages (default 2) never stubbed
- **Env config**: `TOOLRECALL_DEDUP_MIN_CHARS`, `TOOLRECALL_DEDUP_PROTECT_LAST`, `TOOLRECALL_DEDUP_DISABLED`

**Measured (billing-verified, OpenRouter):** on an accumulating agent loop over
10 real SWE-bench Lite instances (80 requests/arm, DeepSeek V4 Flash), the hook
cut **−32.3% prompt tokens / −30% billed cost**, with prefix caching preserved
(effective $/M unchanged). See the [benchmark & methodology](../../bench/litellm_dedup/README.md).

> **Honesty:** the −32% is token/cost savings. Task quality (pass@1) is
> **unverified** — the hook stubs content, and a quality A/B requires a model
> that can actually solve the tasks; a baseline model scored 0 so no dedup
> effect on pass@1 could be measured. The hook removes wasted tokens; it does
> not claim to improve answers.

> **⚠ Known limitation:** LiteLLM currently bypasses `async_pre_call_hook` on the
> Anthropic-format `/v1/messages` endpoint ([#27518](https://github.com/BerriAI/litellm/issues/27518)).
> Route through `/v1/chat/completions` for the hook to fire.

> **Layer note:** Gateway dedup is *lossy compression* — it stubs and hopes the model
> doesn't need the bytes back. For *lossless recall* (serve exact bytes on demand,
> freshness tracking, cross-session state), use the [ToolRecall daemon](../../README.md).

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