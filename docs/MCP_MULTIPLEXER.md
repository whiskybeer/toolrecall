# MCP Multiplexer — Server Registry & Auto-Resolution

> **One daemon to rule all MCP servers.**  
> `toolrecall` runs a single daemon (`daemon.py`) that multiplexes multiple external MCP
> subprocesses — so agents (Hermes, Claude Code, Cursor) only configure `toolrecall mcp`
> instead of every server individually.  
> Server names auto-resolve via the built-in registry — no need to write `command`/`args` for common servers.

## When the multiplexer is (and isn't) worth enabling

The multiplexer is a shared-process optimization, not a default win for
every setup:

**Enable it when:**
- **Several agent sessions share one machine** — the daemon keeps one warm
  subprocess pool per server and shares it across all sessions (see
  [Agent Compatibility](AGENT_COMPATIBILITY.md)). With a single session,
  per-agent MCP config is simpler and equivalent.
- **Your agent has no native multi-server MCP config** — configure exactly
  one MCP entry (`toolrecall mcp`) instead of N servers in every client.
- **You want transparent response caching on MCP tool calls** (RFC 8785
  request keys since 0.8.21). Caution: with `default_ttl` > 0,
  identical-args *write* calls can return cached responses — set
  `ttl = 0` on any server that exposes write tools.

**Skip it when:**
- **Your agent natively manages MCP servers per session** (Hermes
  `mcp_servers` in `config.yaml`, Claude Code `.mcp.json`, Cursor
  `mcp.json`). You already get per-session lifecycle and direct config;
  the multiplexer then only adds an extra hop (`mcp_call` indirection)
  and a merged tool surface.
- **You run few agents and call MCP tools rarely** — check the `mcp_cache`
  table in the stats DB: if it stays empty, the multiplexer is dead weight.
  Disable with `[mcp_multiplex] enabled = false` — the daemon skips the
  multiplexer entirely (no registry, no lazy processes). The transparent
  file cache, the `cache-fs` server, and the forward proxy are unaffected.

Enabled-but-unused costs nothing (lazy start + idle reaper); skipping is
about keeping the tool surface lean, not about RAM.

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
# Built-in (stdlib, no deps): docs, fetch, cache-fs, sequential-thinking, github (opt-in), time
# External (needs uvx): filesystem, git, memory, brave-search, playwright, slack
servers = ["docs", "fetch", "cache-fs"]
idle_minutes = 15
```

Server names are auto-resolved:

| Type | Source | Example |
|------|--------|---------|
| **Built-in** | Ships with ToolRecall (stdlib, zero deps) | `docs`, `fetch`, `cache-fs`, `sequential-thinking`, `github` (opt-in), `time` |
| **External (registry)** | Auto-resolved via `uvx <package>` | `filesystem`, `git`, `memory` |
| **Custom** | Explicit `servers_config` override | Any user-defined command |

### Key Options

| Key | Default | Description |
|-----|---------|-------------|
| `enabled` | `true` | Activates multiplexer |
| `servers` | `["docs", "fetch", "cache-fs"]` | Server names to enable — auto-resolved from registry |
| `idle_minutes` | `15` | Minutes before idle server is killed (RAM saver) |
| `default_ttl` | `60` | Default cache TTL (seconds) for responses |
| `transparent_cache` | `true` | Cache responses transparently (same key = same result) |

### MCP Cache Stats

When the MCP bridge routes tool calls (`read_file`, `write_file`, `patch`, `terminal`), the daemon records `mcp_cache` hits/misses alongside the primary file/terminal cache stats. This appears in `toolrecall stats` and the healthcheck output as `mcp_hits=N, mcp_missed=N`.

**Agent-agnostic:** Every MCP client (Hermes, Claude Code, Cline, OpenCode) that connects through the bridge automatically contributes to `mcp_cache` stats. No per-agent configuration needed.

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
| `docs` | `docs_search`, `docs_get_page` | Full-text search over the FTS5 knowledge index (BM25). Uses the same engine as the bridge's native docs tools. |
| `cache-fs` | `read_file`, `write_file`, `patch`, `terminal` | File/terminal tools backed by the daemon cache. Most useful as a standalone MCP tool set for agents without the OS-level shim — the bridge server (`toolrecall mcp`) already provides these tools natively. |

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

> **Note:** External servers require `uvx` (part of `uv`, installable via `curl -LsSf https://astral.sh/uv/install.sh | sh`). Built-in servers need no extra dependencies.

The registry is defined in [`toolrecall/mcp_registry.py`](https://github.com/whiskybeer/toolrecall/blob/main/toolrecall/mcp_registry.py) — extend it by sending a PR.  
The built-in servers (`toolrecall/mcp_time.py`, `toolrecall/mcp_github.py`, `toolrecall/mcp_seqthink.py`, `toolrecall/mcp_fetch.py`, `toolrecall/mcp_cache_fs.py`, `toolrecall/mcp_docs.py`) are all pure Python stdlib — no external packages needed.

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
