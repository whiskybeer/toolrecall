# MCP Multiplexer

> **One daemon to rule all MCP servers.**  
> `toolrecall` runs a single daemon (`daemon.py`) that multiplexes multiple external MCP
> subprocesses — so agents (Hermes, Claude Code, Cursor) only configure `toolrecall mcp`
> instead of every server individually.

## Architecture

```
            ┌─────────────────────────┐
   agent 1   │   toolrecall daemon     │
   agent 2   │   ┌─────────────────┐   │   ┌────────┐
   agent 3 ──┤──│ MCPMultiplexer  │───┼──→│ github │
            │   │                 │   │   ├────────┤
            │   │  lazy start     │───┼──→│  time  │
            │   │  idle reaper    │   │   ├────────┤
            │   │  auto-reconnect  │───┼──→│ fetch  │
            │   └─────────────────┘   │   └────────┘
            └─────────────────────────┘
```

- **Single daemon** — all agents share one persistent process
- **Lazy loading** — servers boot *only* on first call, not at daemon start
- **Idle timeout** — inactive servers auto-killed after 15 min (configurable)
- **Failure isolation** — one crash doesn't affect others
- **Auto-reconnect** — up to 3 retries on subprocess crash

## Configuration

```toml
# ~/.toolrecall/config.toml
[mcp_multiplex]
enabled = true
transparent_cache = true
default_ttl = 60
servers = []              # empty = all servers allowed
idle_minutes = 15

[mcp_multiplex.servers_config]
# Each entry: { command, args, env, ttl }
time = { command = "python3", args = ["-m", "toolrecall.mcp_time"] }
"sequential-thinking" = { command = "python3", args = ["-m", "toolrecall.mcp_seqthink"] }
fetch = { command = "uvx", args = ["mcp-server-fetch"] }
```

### Key Options

| Key | Default | Description |
|-----|---------|-------------|
| `enabled` | `true` | Activates multiplexer |
| `servers` | `[]` | Allowlist — empty = all; `["github", "time"]` = only these |
| `idle_minutes` | `15` | Minutes before idle server is killed (RAM saver) |
| `default_ttl` | `60` | Default cache TTL (seconds) for responses |
| `transparent_cache` | `true` | Cache responses transparently (same key = same result) |

### Per-Server Override

```toml
[mcp_multiplex.servers_config]
github = { command = "python3", args = ["-m", "toolrecall.mcp_github"],
           env = { GITHUB_TOKEN = "$GITHUB_TOKEN" },
           ttl = 300 }  # 5 min cache for heavy queries
```

## Secrets: `~/.toolrecall/.env`

Server-specific secrets (GitHub tokens, API keys) go here — **not** in `config.toml`:

```env
# ~/.toolrecall/.env
GITHUB_TOKEN=ghp_xxxxxxxxxxxx
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

1. Add entry to `[mcp_multiplex.servers_config]` in `config.toml`
2. Set env vars in `~/.toolrecall/.env` if needed
3. Optionally add to `servers` allowlist
4. Optionally set per-server `ttl` override for cache policy