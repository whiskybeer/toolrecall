# ToolRecall

**Universal Tool-Output Cache & MCP Multiplexer for LLM Agents**

ToolRecall is a caching layer for AI agents (like Claude Code, Hermes, Aider, OpenHands). It intercepts tool calls (file reads, terminal commands, script executions) and returns cached results if nothing has changed. 

The main selling point: **It saves ~80% of input tokens with zero drawbacks for deterministic tasks**, allowing you to run powerful local coding agents much cheaper and faster.

## Why ToolRecall?

LLM agents are notorious for redundant actions. During a debugging loop, an agent might read the same 50KB log file or run the same `git status` command ten times. 
- **API Prompt Caching** (like Anthropic's or OpenAI's) caches the *input prompt* on the server.
- **ToolRecall** prevents the agent from even executing the tool or waiting for output, caching the *tool result* locally.

The two systems stack perfectly. ToolRecall is **100% GDPR compliant**: data never leaves your local machine, there is no telemetry, and everything is stored in a local SQLite database.

## Architecture & Tech Stack

ToolRecall uses a hybrid architecture:
1. **In-Memory LRU Cache** (~0.001ms lookup)
2. **SQLite Database** (~1-5ms lookup, persistent across sessions)
3. **Daemon Process** (Unix Domain Sockets)

**Does "Zero Dependencies" still hold true?**
Yes. The core caching logic (`toolrecall.cache`) uses pure Python standard library (`sqlite3`, `json`, `hashlib`). The `mcp` package is only required if you use the MCP Bridge.

**Could we switch to a faster stack (Rust/Go/Redis)?**
Python + SQLite is already returning cache hits in under 2 milliseconds. The bottleneck in AI workflows is the LLM generation time (seconds) and network overhead. Moving to Rust or Redis would overcomplicate the deployment for a microscopic gain.

## Features

### 1. Tool-Output Caching
- **File Reads**: Auto-invalidates based on `mtime`.
- **Terminal Commands**: Managed via an exact-match allow-list and TTLs (Time-To-Live). E.g., `git status` is cached for 30s, `uname` for 3600s.
- **Code Execution**: Hashes the code content.

### 2. MCP Multiplexer (v0.3.0+)
If you use multiple MCP (Model Context Protocol) servers (e.g., GitHub, Time, Sequential Thinking), starting them per-session wastes RAM and startup time.
The ToolRecall Daemon acts as a **Multiplexer**:
- It runs your MCP servers as persistent subprocesses.
- It lazy-loads them on the first call.
- It shuts them down after 15 minutes of idle time.
- Your agent only needs to connect to **ONE** MCP server: `toolrecall mcp`.

### 3. Local Knowledge Base (Micro-RAG)
ToolRecall includes a SQLite FTS5 (Full-Text Search) engine. It indexes your agent's skills, scripts, and documents locally. 

**Can I query past sessions to avoid repeating mistakes?**
Currently, ToolRecall indexes documents and skills. To search conversational history, your agent should rely on its native `session_search` tool (which Hermes already possesses). However, any *successful workflow or lesson learned* should be written to a `.md` file in `~/.hermes/skills/` — ToolRecall automatically indexes these, ensuring the agent retrieves the best approach in future sessions!

## Installation

```bash
pip install toolrecall[mcp]
```

Start the daemon (runs in the background, recommended as a systemd user service):
```bash
toolrecall daemon
```

## Usage Examples

### With Claude Code (Local Agent)
Claude Code supports MCP. Simply add ToolRecall to your Claude Code config to instantly benefit from cached file reads and terminal commands:
```bash
claude mcp add toolrecall -- uv run python -m toolrecall.mcp_server
```

### With Python Agents
```python
from toolrecall.client import cached_read, cached_terminal, mcp_call

# Instant (if mtime hasn't changed)
content = cached_read("/path/to/large_file.log")

# Instant (cached for 30s via TTL)
git_stat = cached_terminal("git status")

# Routed through the multiplexer to a persistent GitHub MCP server
issues = mcp_call("github", "list_issues", {"owner": "user", "repo": "repo"})
```

## When NOT to use caching

ToolRecall is designed to fail safe, but caching should be bypassed (`ttl=0` or `bypass_cache=True`) when:
- Reading real-time data streams (sensor data, live market tickers).
- Executing state-changing commands (e.g., `git push`, `npm install`, or `curl -X POST`).
- Running tests where the underlying environment changed outside of the tracked files.

## Configuration

Settings are managed in `~/.toolrecall/config.toml`.

```toml
[mcp]
# Allow-list for file reading over MCP
allowed_paths = ["~/.hermes/skills", "~/projects"]
allow_terminal = false # Keep disabled for safety

[mcp_multiplex]
enabled = true
idle_minutes = 15

[mcp_multiplex.servers_config]
github = { command = "npx", args = ["-y", "@modelcontextprotocol/server-github"] }
```

Secrets (like `GITHUB_PERSONAL_ACCESS_TOKEN`) should be placed in `~/.toolrecall/.env` and are securely loaded by the daemon before launching subprocesses.
