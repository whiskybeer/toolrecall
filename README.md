# ToolRecall: The L1 Cache for LLM Agents

**An $O(N^2)$ Context Mitigation & MCP Multiplexer Middleware**

ToolRecall is a deterministic middleware layer (API Gateway/WAF) for autonomous AI agents like Claude Code, Cursor, Aider, and Hermes. It sits between the agent and the operating system, caching tool executions and managing external MCP servers via Unix Domain Sockets (IPC).

The core value proposition: **It breaks the $O(N^2)$ context snowball effect.** 
In a recent benchmark, ToolRecall saved **141.1 million input tokens (~$282)** in a single 13-hour session by serving tool results from a local SQLite FTS5 database in 1.5ms.

👉 **[Read the 141M Token Benchmark Case Study](BENCHMARK.md)**

---

## The Core Problem: The Context Snowball

LLM context windows are stateless. Every time an agent reads a 10,000-token file to debug an issue, those 10,000 tokens are added to the history. If the session continues for 100 turns, those same 10,000 tokens are re-transmitted to the API 100 times, costing 1,000,000 billed input tokens.

**The ToolRecall Solution (Micro-RAG):**
1. Agents read the file once.
2. The agent is instructed to *drop* the file dump from its active context window to save space.
3. If the agent needs the file again hours later, ToolRecall serves the *exact byte-for-byte output* from its local SQLite cache instantly.
4. If the file is modified (`write_file`), ToolRecall's locking mechanism instantly invalidates the cache.

**Zero Hallucination:** Unlike Vector-DBs that use LLMs to summarize older context (which introduces hallucinations), ToolRecall returns raw `stdout` and JSON data deterministically.

---

## Architecture & Security (The "Armor")

ToolRecall is not just a cache; it is a **Security Sandbox (WAF)** for LLM agents to mitigate Prompt Injections.

1. **Daemon-Based (IPC):** ToolRecall runs as a persistent daemon. Agents communicate with it via Unix Domain Sockets (`/run/user/1004/toolrecall.sock`). There are no open TCP ports (immune to SSRF and port scanning).
2. **Hard Allowlist:** Instead of trusting the LLM's system prompt to "not read passwords," ToolRecall enforces a hard Python-level allowlist (e.g., `["~/projects", "~/.hermes"]`). If a prompt injection tricks the agent into reading `~/.ssh/id_rsa`, the middleware blocks it with `Access Denied` before the file is ever touched.
3. **Terminal Gating:** By default, `allow_terminal = false`. Read-operations are allowed, but shell commands are filtered unless explicitly whitelisted by the developer.

---

## Features (v0.3.0)

### 1. Byte-Exact Tool Caching
- **File Cache:** Invalidates instantly based on file modifications (`mtime`) and internal invalidation locks.
- **Terminal Cache:** Caches read-only shell commands based on TTLs (e.g., `git status` for 30s).

### 2. The MCP Multiplexer
Running 5 different MCP Servers (GitHub, Time, Fetch, etc.) per session wastes RAM (~600MB) and startup time.
- ToolRecall acts as a persistent host for your MCP servers.
- **Lazy Loading:** Servers boot in 0.01s only when a tool is requested.
- **Idle Timeout:** Servers are killed after 15 minutes of inactivity to recover RAM (dropping daemon footprint from 490MB to 11MB).
- Agents only connect to **ONE** server: `toolrecall mcp`.

---

## Installation & Quickstart

**Requirements:** Python 3.10+, standard SQLite.

```bash
# 1. Install via pip
pip install toolrecall[mcp]

# 2. Initialize default config and .env (Creates ~/.toolrecall/)
toolrecall init

# 3. Start the Daemon in the background
toolrecall daemon &
```

### Usage: Claude Code (Drop-in Replacement)
To instantly make Claude Code 80% cheaper and faster, route it through ToolRecall:
```bash
claude mcp add toolrecall -- uv run python -m toolrecall.mcp_server
```

### Configuration & Secrets
Settings are managed in `~/.toolrecall/config.toml`.
Secrets (like `GITHUB_PERSONAL_ACCESS_TOKEN`) should be placed in `~/.toolrecall/.env`. The daemon securely loads them before launching subprocesses. No secrets are ever stored in Git or passed via the LLM context.

```toml
[mcp]
allowed_paths = ["~/projects", "~/.hermes/skills"]
allow_terminal = false # Security: Prevents Prompt Injection executions
default_ttl = 60 # Default TTL for MCP requests in seconds

[mcp_multiplex]
enabled = true
idle_minutes = 15

[mcp_multiplex.servers_config]
github = { command = "npx", args = ["-y", "@modelcontextprotocol/server-github"], ttl = 60 }
# Set ttl = 0 to entirely bypass caching for dynamic endpoints (like CI logs or real-time data)
```

## Cross-Platform Support
ToolRecall uses **Unix Domain Sockets (IPC)** for daemon communication, making it highly secure.
- **Linux:** Uses `XDG_RUNTIME_DIR` (e.g., `/run/user/1000/toolrecall.sock`).
- **macOS / Windows 10+:** Falls back automatically to `~/.toolrecall/toolrecall.sock`. Windows Named Pipes/AF_UNIX are supported.

## Status
**Experimental.** ToolRecall is currently used in heavy autonomous agent workflows. Before deploying it in production CI/CD environments, ensure your allowlist is strictly scoped.