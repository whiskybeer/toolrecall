# MCP Multiplexer — Server Registry & Auto-Resolution

> **One daemon to rule all MCP servers.**  
> `toolrecall` runs a single daemon (`daemon.py`) that multiplexes multiple external MCP
> subprocesses — so agents (Hermes, Claude Code, Cursor) only configure `toolrecall mcp`
> instead of every server individually.  
> Server names auto-resolve via the built-in registry — no need to write `command`/`args` for common servers.

## Architecture

```mermaid
flowchart LR
    subgraph Agents["Agents"]
        A1["agent 1"]
        A2["agent 2"]
        A3["agent 3 ···"]
    end

    subgraph TR["toolrecall daemon"]
        MUX["MCPMultiplexer<br/>lazy start · idle reaper<br/>auto-reconnect"]
    end

    subgraph Downstream["Downstream Servers"]
        G["github"]
        T["time"]
        F["fetch"]
        E["···"]
    end

    Agents --> MUX
    MUX --> G
    MUX --> T
    MUX --> F
    MUX --> E


- **Single daemon** — all agents share one persistent process
- **Lazy loading** — servers boot *only* on first call, not at daemon start
- **Idle timeout** — inactive servers auto-killed after 15 min (configurable)
- **Failure isolation** — one crash doesn't affect others
- **Auto-reconnect** — up to 3 retries on subprocess crash

## Configuration

```toml
# ~/.config/toolrecall/toolrecall.toml
[mcp_multiplex]
enabled = true
transparent_cache = true
default_ttl = 60
# Server names: auto-resolved via built-in registry.
# Built-in (stdlib, no deps): time, github, sequential-thinking, fetch
# External (needs uvx): filesystem, git, memory, brave-search, playwright, slack
servers = ["time", "github", "sequential-thinking"]
idle_minutes = 15
```

Server names are auto-resolved:

| Type | Source | Example |
|------|--------|---------|
| **Built-in** | Ships with ToolRecall (stdlib, zero deps) | `time`, `github`, `sequential-thinking`, `fetch` |
| **External (registry)** | Auto-resolved via `uvx <package>` | `filesystem`, `git`, `memory` |
| **Custom** | Explicit `servers_config` override | Any user-defined command |

### Key Options

| Key | Default | Description |
|-----|---------|-------------|
| `enabled` | `true` | Activates multiplexer |
| `servers` | `["time", "github", "sequential-thinking"]` | Server names to enable — auto-resolved from registry |
| `idle_minutes` | `15` | Minutes before idle server is killed (RAM saver) |
| `default_ttl` | `60` | Default cache TTL (seconds) for responses |
| `transparent_cache` | `true` | Cache responses transparently (same key = same result) |

### Custom Server Override

To override a registry entry or add a server not in the registry:

```toml
[mcp_multiplex.servers_config]
github = { command = "npx", args = ["-y", "@modelcontextprotocol/server-github"],
           env = { GITHUB_TOKEN = "$GITHUB_TOKEN" },
           ttl = 300 }
my-custom-server = { command = "docker", args = ["run", "--rm", "my-server"] }
```

This overrides the auto-resolved `github` with the official npm package and adds a custom server.

## Server Registry

### Built-in Servers

Shipped with ToolRecall, no external dependencies:

| Server | Tools | Description |
|--------|-------|-------------|
| `time` | `get_time`, `list_timezones` | Current time in any timezone. Stdlib only. |
| `github` | `create_repository`, `create_or_update_file`, `push_files`, `list_commits` | GitHub API via stdlib `urllib`. Needs `GITHUB_TOKEN` in `.env`. |
| `sequential-thinking` | `think_step`, `analyze`, `validate_reasoning` | Reasoning step validation, contradiction detection. No network. |
| `fetch` | `fetch_url`, `fetch_head`, `fetch_headers` | Fetch URLs — stdlib only (`urllib.request`). 500KB limit configurable via `TOOLRECALL_FETCH_MAX_BYTES` env var. |

### External Servers

Auto-resolved via `uvx` when listed in `servers`:

| Server | Package | Description |
|--------|---------|-------------|
| `filesystem` | `mcp-server-filesystem` | Read/write files with path allowlist |
| `git` | `mcp-server-git` | Git operations (log, diff, status) |
| `memory` | `mcp-server-memory` | Persistent knowledge graph |
| `everything` | `mcp-server-everything` | Test/demo server with every tool type |
| `brave-search` | `@anthropic/mcp-server-brave-search` | Web search via Brave API |
| `playwright` | `@playwright/mcp` | Browser automation |
| `slack` | `mcp-server-slack` | Slack workspace integration |

> **Note:** External servers require `uvx` (part of `uv`, installable via `pip install uv` or `curl -LsSf https://astral.sh/uv/install.sh | sh`). Built-in servers need no extra dependencies.

The registry is defined in [`toolrecall/mcp_registry.py`](https://github.com/whiskybeer/toolrecall/blob/main/toolrecall/mcp_registry.py) — extend it by sending a PR.  
The built-in servers (`toolrecall/mcp_time.py`, `toolrecall/mcp_github.py`, `toolrecall/mcp_seqthink.py`, `toolrecall/mcp_fetch.py`) are all pure Python stdlib — no external packages needed.

## Secrets: `~/.toolrecall/.env`

Server-specific secrets (GitHub tokens, API keys) go here — **not** in `config.toml`:

```env
# ~/.toolrecall/.env
GITHUB_TOKEN=<your-token>
```

Loaded by `MCPClientSession._start()` on each server boot.

## Lifecycle

```python
mux = MCPMultiplexer(cfg)
mux.start()                  # load configs, start reaper (no servers)
mux.call("github", "list_issues", {"owner": "whiskybeer", "repo": "toolrecall"})
# → first call: ~2s start + init handshake
# → subsequent calls: ~0.01s (already running)
# → after 15min idle: process killed, next call restarts
```

## When to Add a Server

1. **Built-in or known server:** Add name to `servers` list in config — auto-resolved
2. **Custom server:** Add entry to `[mcp_multiplex.servers_config]` in `config.toml`
3. Set env vars in `~/.toolrecall/.env` if needed
4. Optionally set per-server `ttl` override for cache policy
