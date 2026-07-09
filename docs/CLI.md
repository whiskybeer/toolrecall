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
- **Purpose:** Index a specific directory (e.g., an Obsidian vault) into the FTS5 knowledge DB.
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
  - `GET /read?path=X` — cached_read demo
  - `GET /term?cmd=X` — cached_terminal demo
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
  - `toolrecall shim --install` — install the shim
  - `toolrecall shim --remove` — remove the shim
  - `toolrecall shim --status` — check if shim is installed

### `toolrecall nginx`

- **File:** `cli.py : cmd_nginx()`
- **Purpose:** Generate an nginx reverse-proxy config for the forward proxy.
- **Uses:** `[nginx]` section in `config.toml` (domain, SSL, etc.).

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

## See Also

- [Configuration Reference](CONFIG_REFERENCE.md) — `config.toml`, `config.py`, env vars
- [MCP Multiplexer](MCP_MULTIPLEXER.md) — server registry, `mcp list`, auto-resolution
- [Hermes Transparent Cache](HERMES_TRANSPARENT_CACHE.md) — agent-side integration via the OS-level .pth shim