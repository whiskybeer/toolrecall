# ToolRecall + Odysseus Integration

Agent tool and MCP server caching for the [Odysseus AI workspace](https://github.com/pewdiepie-archdaemon/odysseus).

## Setup

```bash
pip install toolrecall
```

## Agent Tool Cache

Wrap Odysseus's tool execution with transparent caching:

```python
from toolrecall.adapters.odysseus import install_agent_cache

# Call near startup — wraps tool_execution.py
install_agent_cache()
```

Every tool block execution (shell, script, search, web_fetch, etc.) is cached by tool name + arguments hash. Repeat calls serve from cache — zero re-execution.

## MCP Server Cache

Wrap the MCP manager for cached built-in server results (email, memory, rag, image_gen):

```python
from toolrecall.adapters.odysseus import install_mcp_cache
from src.mcp_manager import McpManager

mgr = McpManager()
install_mcp_cache(mgr)
```

## Decorator (per-function)

For fine-grained control, use the decorator directly:

```python
from toolrecall.adapters.odysseus import cached_tool

@cached_tool(ttl=300)
def search_web(query: str) -> str:
    return external_api_call(query)  # Only on cache miss
```

Async tools also supported:

```python
from toolrecall.adapters.odysseus import cached_async_tool

@cached_async_tool(ttl=600)
async def fetch_data(url: str) -> dict:
    return await http_get(url)
```

## Key Properties

- **Graceful bypass:** Daemon not running → all calls pass through, no crash
- **Namespace isolation:** `odysseus` — keys never collide with other adapters
- **Async-safe:** Matching async/sync decorators
- **No new deps:** Uses `toolrecall.cache` — already installed with the package
