# ToolRecall + Google ADK (Agent Development Kit)

> **Date:** July 2026 · ADK Python 2.0 (20.4k ⭐, google/adk-python)
> **Principle:** ToolRecall is agent-agnostic. No ADK code changes required.

## Table of Contents

- [Why ADK?](#why-adk)
- [Three Integration Paths](#three-integration-paths)
  - [1. Forward Proxy — no code, maximum impact](#1-forward-proxy---no-code-maximum-impact)
  - [2. Agent-Agnostic Runtime Patch — file/subprocess level](#2-agent-agnostic-runtime-patch---filesubprocess-level)
  - [3. Embedded Library — deepest integration](#3-embedded-library---deepest-integration)
- [ADK-Specific Considerations](#adk-specific-considerations)
- [Expected Benchmark Impact](#expected-benchmark-impact)
- [Limitations](#limitations)

---

## Why ADK?

ADK (Python) has **no built-in caching** for tool outputs. Every tool call runs through `run_async()` fresh — even when the agent reads the same file 10× in a debug loop. The runner has no `key → value` memory at the tool level.

ADK relies on **Agent Runtime** for **long-running agents (up to 7 days)**. The longer a session runs, the more tool calls repeat (same config, same status, same file).

ToolRecall fills this gap — without changing ADK internally.

---

## Three Integration Paths

### 1. Forward Proxy — no code, maximum impact

| Aspect | Value |
|--------|-------|
| **ADK changes** | ❌ None |
| **Token savings** | **100%** on API cache hit |
| **Setup** | `toolrecall serve` + set `GEMINI_BASE_URL` |
| **Limit** | Caches only byte-identical API requests |

ADK sends tool results back to Gemini. When the agent sends the same prompt + same history in a long-running session, the **Forward Proxy answers from the local cache** — Gemini is never contacted.

**Setup:**

```bash
# Start daemon (once)
toolrecall daemon &

# Start forward proxy
toolrecall serve

# Point ADK at the proxy
export GEMINI_BASE_URL=http://localhost:8569/v1
```

This works because ADK (like all modern frameworks) respects `GEMINI_BASE_URL` / `OPENAI_BASE_URL` as the API base URL.

**When it applies:**
- Agent repeatedly polls the same status endpoint (long-running)
- Repeated generation with identical prompt
- Batch processing: same request, different params → cache miss, but **same responses** → hit

### 2. Agent-Agnostic Runtime Patch — file/subprocess level

| Aspect | Value |
|--------|-------|
| **ADK changes** | ❌ None |
| **What it caches** | `builtins.open()` + `subprocess.run()` + `subprocess.Popen()` |
| **Setup** | `.pth` file installed via `toolrecall shim --install` |

See the [Hermes Transparent Cache guide](HERMES_TRANSPARENT_CACHE.md) for full documentation on the OS-level `.pth` shim.

Patching happens at the Python level: **every** `open(path, 'r')` in ADK, in custom tools, in MCP server processes is automatically routed through ToolRecall.

**Example — ADK `FunctionTool` reading a file:**

```python
# ADK code — ToolRecall patches builtins.open()
@function_tool
def read_config() -> str:
    with open("config.yaml") as f:  # ← TR intercepts here
        return f.read()
```

The patch covers:
1. `builtins.open()` → mtime-based caching (file reads)
2. `subprocess.run()` → TTL-based caching (read-only commands like `ls`, `hostname`)
3. `subprocess.Popen()` is left untouched (background processes can't cache meaningfully)
4. Security: binary files, `.env`, `.ssh` etc. are never cached

**ADK advantage:** Because ADK is written in Python and defines tools as Python functions, **every tool call benefits automatically** — no tool needs to know TR exists.

### 3. Embedded Library — deepest integration

|| Aspect | Value |
||--------|-------|
|| **ADK changes** | ⚡ Minimal — wrappers around ADK tools |
|| **Setup** | `from toolrecall import cached_read, cached_write` in custom tools |
|| **Control** | Full — invalidation, mtime-check, write dedup |

See the [CLI Reference](CLI.md) and [API usage in `toolrecall/cache.py`](https://github.com/whiskybeer/toolrecall/blob/main/toolrecall/cache.py) for the full API.

**Wrapper pattern for ADK `FunctionTool`:**

```python
from google.adk import Agent, function_tool
from toolrecall import cached_read, cached_write, invalidate_file
import os

@function_tool
def read_file(path: str) -> str:
    """Read a file with caching."""
    result = cached_read(path)
    if "error" in result:
        raise RuntimeError(result["error"])
    return result["content"]

@function_tool
def write_file(path: str, content: str) -> str:
    """Write a file — skips if content is identical."""
    result = cached_write(path, content)
    if "error" in result:
        raise RuntimeError(result["error"])
    return f"Written {len(content)} bytes"
```

**MCPToolset wrapper pattern:**

ADK's `MCPToolset` wraps MCP tools into ADK tools. The Forward Proxy caches *API responses*, but not the MCP tool calls themselves. For the latter you'd need an MCP proxy between ADK and the MCP server — ToolRecall's MCP Bridge doesn't apply directly because ADK brings its own `mcp` client.

| Path | Works? | Reason |
|------|:---:|--------|
| TR MCP Bridge (direct) | ❌ | ADK uses its own mcp session, not TR |
| TR Forward Proxy | ✅ | Caches API responses before they reach Gemini |
| TR Embedded Library | ✅ | In `FunctionTool` wrappers for file operations |
| TR Agent-Agnostic Patch | ✅ | Catches `open()` inside MCP server processes |

---

## ADK-Specific Considerations

### Long-Running Agents (7 days)

ADK's Agent Runtime runs for up to 7 days. During that time:

- **Without TR:** Every turn sends the same context + tool results → full token cost
- **With TR (Forward Proxy):** Byte-identical API responses are cached. **Gemini prefix caching** + TR Forward Proxy = double savings
- **With TR (Agent-Agnostic):** Files the agent keeps reading across days (config, status, logs) are read from disk only once

### MCP in ADK

ADK's `MCPToolset` supports **stdio**, **SSE**, and **StreamableHTTP** for MCP servers. The MCP architecture in ADK:

1. `MCPToolset.__init__()` → creates `MCPSessionManager`
2. `get_tools()` → lists tools via MCP protocol
3. Each tool call → `run_async()` → MCP server via `MCPSessionManager`

ToolRecall **cannot** insert itself between ADK and the MCP server (ADK has no plugin hook for MCP calls). However:

- **The Forward Proxy caches the Gemini API responses** that contain the MCP results
- **The Agent-Agnostic Patch caches `open()` calls inside the MCP server process** itself (when the MCP server runs in Python)

### Multi-Agent Orchestration

ADK 2.0 has **graph-based workflows** and the **Coordinator-Specialist pattern**. With one coordinator + N specialists:

- **Without TR:** Every specialist reads the same files → N× token cost
- **With TR (Forward Proxy):** Each specialist is a separate LLM call → API cache only hits on identical requests
- **With TR (Agent-Agnostic):** All specialists share the same daemon → file cache is shared → first read caches, all subsequent hits

---

## Expected Benchmark Impact

| Scenario | Without TR | With TR (Forward Proxy) | With TR (Agent-Agnostic) |
|----------|:---------:|:-----------------------:|:------------------------:|
| Simple Chat (20 turns) | 100% | ~40-60% | ~40-60% |
| Debug Loop (read→edit→test ×10) | 100% | ~27-35% | ~27-35% |
| Long-Running (7 days, daily check) | 100% | ~5-15% (massive repetition) | ~15-25% |
| Batch Processing (N configs) | 100% | ~20-40% | ~20-40% |
| Code Review (10 files, 5 iterations) | 100% | ~30-45% | ~25-35% |

Forward Proxy values are **estimates** based on ToolRecall benchmarks (55K tokens in 13h real Hermes debugging). Exact ADK benchmarks do not exist yet.

---

## Limitations

1. **No direct MCP call cache** — ADK has no plugin hook for MCP `call_tool`. The Forward Proxy caches API results, not the MCP transport layer.
2. **Gemini prefix caching can partially replace TR** — Google caches frequently-used prefixes server-side. TR still provides value: (a) TR caches *complete* responses (not just prefixes), (b) TR works locally → 0 latency, (c) TR also caches non-Gemini providers (if ADK is used with third-party models).
3. **Embedded Library requires manual wrapping** — for maximum control, each tool must be wrapped individually. The Agent-Agnostic Patch is the simpler path.
4. **MCP servers in non-Python languages** — ADK supports MCP servers in any language. The Agent-Agnostic Patch only patches Python processes. Node.js/Rust/Go MCP servers are not covered.