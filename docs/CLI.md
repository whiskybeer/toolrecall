# CLI Reference — `toolrecall/cli.py`

The CLI entry point (`toolrecall`) dispatches commands via `toolrecall/cli.py`.  
Each subcommand is a standalone function — the dispatch table maps `sys.argv[1]` to the function.

## Architecture

```
toolrecall <command> [subcommand] [options]
```

The dispatcher in `cli.py` maps the first argument to a `cmd_*` function:

| Call | Function | File |
|------|----------|------|
| `toolrecall setup` | `cmd_setup()` | `toolrecall/cli.py` |
| `toolrecall restart` | `cmd_restart()` | `toolrecall/cli.py` |
| `toolrecall init` | `cmd_init()` | `toolrecall/cli.py` |
| `toolrecall status` | `cmd_status()` | `toolrecall/cli.py` |
| `toolrecall stats` | `cmd_stats()` | `toolrecall/cli.py` |
| `toolrecall invalidate` | `cmd_invalidate()` | `toolrecall/cli.py` |
| `toolrecall reset-stats` | `cmd_reset_stats()` | `toolrecall/cli.py` |
| `toolrecall index` | `cmd_index()` | `toolrecall/cli.py` |
| `toolrecall index-memory` | `cmd_index_memory()` | `toolrecall/cli.py` |
| `toolrecall index-dir` | `cmd_index_dir()` | `toolrecall/cli.py` |
| `toolrecall config-set` | `cmd_config_set()` | `toolrecall/cli.py` |
| `toolrecall daemon` | `cmd_daemon()` | `toolrecall/cli.py` |
| `toolrecall serve` | `cmd_serve()` | `toolrecall/cli.py` |
| `toolrecall debug` | `cmd_debug()` | `toolrecall/cli.py` |
| `toolrecall mcp` | `cmd_mcp()` → dispatches subcommands | `toolrecall/cli.py` |
| `toolrecall mcp list` | `cmd_mcp_list()` | `toolrecall/cli.py` |
| `toolrecall shim` | `cmd_shim()` | `toolrecall/cli.py` |
| `toolrecall nginx` | `cmd_nginx()` | `toolrecall/cli.py` |
| `toolrecall replay` | `cmd_replay()` → dispatches subcommands | `toolrecall/cli.py` |
| `toolrecall turso` | `cmd_turso()` | `toolrecall/cli.py` |
| `toolrecall context` | `cmd_context()` → dispatches subcommands | `toolrecall/cli.py` |

Each function imports its dependencies lazily — running `toolrecall status` does not load `daemon.py` or `proxy.py`.

## Subcommands

### `toolrecall init`

- **File:** `cli.py : cmd_init()`
- **Purpose:** Interactive first-time setup. Creates `~/.config/toolrecall/toolrecall.toml` and `~/.toolrecall/.env`.
- **Security:** Shows a default-deny path banner, asks the user which directories the agent may read.
- **Idempotent:** Does not overwrite existing config files.

### `toolrecall status`

- **File:** `cli.py : cmd_status()`
- **Purpose:** Show cache hit/miss statistics from the daemon. Falls back to direct SQLite if no daemon is running.
- **Output:** Human-readable table with hits, misses, hit rate, tokens intercepted per cache layer.

### `toolrecall stats`

- **File:** `cli.py : cmd_stats()`
- **Purpose:** Same as `status` but returns JSON-formatted detailed statistics.

### `toolrecall invalidate`

- **File:** `cli.py : cmd_invalidate()`
- **Purpose:** Clear all caches (file, terminal, MCP, script). Uses daemon if running, else direct SQLite.

### `toolrecall reset-stats`

- **File:** `cli.py : cmd_reset_stats()`
- **Purpose:** Reset hit/miss counters without clearing cache entries. Useful for benchmarking.

### `toolrecall index`

- **File:** `cli.py : cmd_index()`
- **Purpose:** Build/update the FTS5 knowledge database from configured `scan_dirs`.
- **Options:** `--memory` also indexes agent memory stores (MEMORY.md, USER.md).

### `toolrecall index-memory`

- **File:** `cli.py : cmd_index_memory()`
- **Purpose:** Index agent persistent memory stores into the knowledge DB separately.
- **Options:** `--source label` sets a custom source label (default: `agent-memory`).

### `toolrecall index-dir`

- **File:** `cli.py : cmd_index_dir()`
- **Purpose:** Index a specific directory (e.g., an Obsidian vault) into the FTS5 knowledge DB for full-text search (`docs_search()`). This is **not** file-cache pre-warming — the daemon's file/terminal cache warms naturally as files are read during normal use.
- **Options:** `--source label` overrides the auto-detected source label (default: basename of the directory).

### `toolrecall config-set`

- **File:** `cli.py : cmd_config_set()`
- **Purpose:** Set a config value in `~/.config/toolrecall/toolrecall.toml`.
- **Usage:** `toolrecall config-set <section.key> <value>`
- **Parsing:** Auto-detects booleans, integers, floats, lists (`[...]`), and strings.
- **Note:** Uses the built-in TOML serializer — no external dependencies needed.

### `toolrecall daemon`

- **File:** `cli.py : cmd_daemon()`
- **Purpose:** Start the cache daemon (background or foreground).
- **Also starts:** MCP bridge (stdin/stdout) and forward proxy (`:8569`).
- **Subcommands:**
  - `toolrecall daemon` — start in background (detached)
  - `toolrecall daemon --foreground` — start in terminal (for debugging)
  - `toolrecall daemon --stop` — stop the running daemon
  - `toolrecall daemon --status` — check daemon status and PID

### `toolrecall serve`

- **File:** `cli.py : cmd_serve()`
- **Purpose:** Start standalone forward proxy (caches LLM API responses).
- **Also started automatically** with `toolrecall daemon`. Use standalone for custom ports.
- **Options:** `--port PORT` (default: 8569, or `TOOLRECALL_FORWARD_PORT` env var).

### `toolrecall debug`

- **File:** `cli.py : cmd_debug()`
- **Purpose:** Start minimal debug/demo server on `:8570`.
- **Endpoints:**
  - `GET /read?path=X` — cached file read demo
  - `GET /term?cmd=X` — cached terminal demo
  - `GET /stats` — cache statistics
  - `GET /health` — daemon status

### `toolrecall mcp`

- **File:** `cli.py : cmd_mcp()`
- **Purpose:** MCP Bridge entry point. Dispatches subcommands.
- **Subcommands:**
  - `toolrecall mcp` — start the MCP Bridge (stdio → daemon). Connect any MCP agent by adding `toolrecall mcp` to its MCP config.
  - `toolrecall mcp list` — list all registered MCP servers with their source (builtin/external), command, and args. Also warns if `uvx` is not installed.

### `toolrecall shim`

- **File:** `cli.py : cmd_shim()`
- **Purpose:** Install/uninstall OS-level cache shim.
- **What it does:** Installs a `.pth` file in site-packages that auto-imports `toolrecall.shim`, monkey-patching `open()` and `subprocess.run()` in every Python process.
- **Usage:**
  - `toolrecall shim --install` — install shim into current Python env
  - `toolrecall shim --install --venv ~/.hermes/hermes-agent/venv` — install into a specific venv
  - `toolrecall shim --uninstall` — remove shim
  - `toolrecall shim --status` — check if shim is installed
  - `toolrecall shim --status --venv ~/.hermes/hermes-agent/venv` — check in a specific venv
- **⚠️  Important when using `pipx` or `uv tool install`:** The shim is installed into the **current Python environment**. If toolrecall is installed via `pipx` or `uv tool install`, that's an isolated environment — the shim won't activate in your agent's Python runtime. Use `--venv` to target the right venv, or run `toolrecall setup` which auto-detects agent venvs.

### `toolrecall nginx`

- **File:** `cli.py : cmd_nginx()`
- **Purpose:** Generate an nginx reverse-proxy config for the forward proxy.
- **Uses:** `[nginx]` section in `config.toml` (domain, SSL, etc.).

### `toolrecall setup`

- **File:** `cli.py : cmd_setup()`
- **Purpose:** One-shot installation: creates config, systemd user service,
  OS-level `.pth` shim, and starts the daemon. Detects installed agents
  (Hermes, OpenCode) and wires up the MCP bridge automatically.
- **Idempotent:** Safe to re-run — skips existing configs.
- **Auto-start:** After setup, every `toolrecall` command auto-starts the
  daemon if it isn't running.

### `toolrecall restart`

- **File:** `cli.py : cmd_restart()`
- **Purpose:** Health check + clean daemon restart. Verifies config integrity
  before restarting the systemd service.

### `toolrecall replay`

- **File:** `cli.py : cmd_replay()` → dispatches subcommands
- **Purpose:** Record/replay mode for deterministic CI testing.
- **Subcommands:**
  - `toolrecall replay record <scenario>` — start recording
  - `toolrecall replay replay <scenario>` — start replaying
  - `toolrecall replay stop` — stop recording/replaying
  - `toolrecall replay status` — show current mode
  - `toolrecall replay list` — list recorded scenarios
  - `toolrecall replay show <scenario>` — show recorded calls
  - `toolrecall replay export <scenario>` — export as JSON
  - `toolrecall replay import <file.json>` — import from JSON
  - `toolrecall replay delete <scenario>` — delete scenario
- **Full reference:** [Replay Mode](REPLAY_MODE.md)

### `toolrecall turso`

- **File:** `cli.py : cmd_turso()`
- **Purpose:** Turso Cloud sync management.
- **Subcommands:**
  - `toolrecall turso init` — create Turso database + generate token
  - `toolrecall turso enable` — enable background sync
  - `toolrecall turso disable` — disable background sync
  - `toolrecall turso status` — show sync status
- **Full reference:** [libSQL Backend](LIBSQL_COMPARISON.md)

### `toolrecall context`

- **File:** `cli.py : cmd_context()` → dispatches subcommands
- **Purpose:** Query context tracker state without an MCP agent.
- **Subcommands:**
  - `toolrecall context status` — show checkpoint, dirty/clean/stale counts
  - `toolrecall context stale` — list files that were read then overwritten
    (content in context is provably wrong)
  - Options for `stale`: `--format json|table`, `--quiet` (pipeable paths)
  - Exit codes: 0 = nothing stale, 1 = stale files found, 2 = daemon error
- **Full reference:** [Context Stale](CONTEXT_STALE.md), [Context Tracker](CONTEXT_TRACKER.md)

## Key Source Files Referenced

| CLI Command | Backend Module |
|------------|----------------|
| `status`, `stats`, `invalidate` | `toolrecall/client.py` (daemon IPC), `toolrecall/cache.py` (direct SQLite) |
| `daemon` | `toolrecall/daemon.py` |
| `serve`, `debug` | `toolrecall/proxy.py` |
| `mcp` | `toolrecall/mcp_bridge.py` (bridge), `toolrecall/mcp_registry.py` (list) |
| `index`, `index-memory`, `index-dir` | `toolrecall/docs.py` |
| `config-set` | `toolrecall/config.py` |
| `init` | Direct file writes to `~/.toolrecall/` |
| `shim` | `toolrecall/shim.py` |
| `replay` | `toolrecall/replay.py` |
| `turso` | Direct REST + config writes |
| `context` | `toolrecall/context_tracker.py` |
| `setup`, `restart` | `toolrecall/cli.py` (setup/restart logic) |

## See Also

- [Configuration Reference](CONFIG_REFERENCE.md) — `config.toml`, `config.py`, env vars
- [MCP Multiplexer](MCP_MULTIPLEXER.md) — server registry, `mcp list`, auto-resolution
- [Hermes Transparent Cache](HERMES_TRANSPARENT_CACHE.md) — agent-side integration via the OS-level .pth shim
- [Replay Mode](REPLAY_MODE.md) — record/replay tool calls for deterministic CI
- [Context Stale](CONTEXT_STALE.md) — provably stale file detection
- [libSQL Backend](LIBSQL_COMPARISON.md) — Turso Cloud sync commands