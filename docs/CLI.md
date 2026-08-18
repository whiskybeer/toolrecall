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
| `toolrecall stop` | `cmd_stop()` | `toolrecall/cli.py` |
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
- **Purpose:** Install/uninstall/inspect the OS-level cache shim (agent-agnostic).
- **What it does:** Installs `tr_shim.pth` into a target venv's site-packages so
  every Python process there auto-imports `toolrecall.shim`. **Opt-in, default
  off** — interactive-install prompts `[y/N]` (default N); use `--yes`/`-y` to
  skip. The "active" confirmation is only printed after a neutral-cwd
  `import toolrecall.shim` probe passes.
- **Agent type → mechanism:**
  - Python agents in their own venv (Hermes, Codex, OpenCode, Cline) → shim.
  - Non-Python agents (Claude Code, Cursor, Windsurf) → MCP bridge (`toolrecall mcp`).
  - See `docs/HERMES_TRANSPARENT_CACHE.md`.
- **Usage:**
  - `toolrecall shim --install` — install into the current Python env (opt-in)
  - `toolrecall shim --install --venv <path>` — install into a specific venv
    (accepts a venv root or its `bin/python`; probe-verified)
  - `toolrecall shim --install --all` — discover every venv and install (opt-in)
  - `toolrecall shim --uninstall [--venv <path>|--all]` — remove the shim
  - `toolrecall shim --status [--venv <path>|--all]` — per-venv: package present,
    `.pth` present, and `probe: pass` (verified) or `probe: FAIL`
  - Unknown flags → usage + non-zero exit (no silent swallow)
- **⚠️  Important when using `pipx` or `uv tool install`:** The shim is installed into the **current Python environment** only. If toolrecall is installed via `pipx` or `uv tool install`, that's an isolated environment — the shim won't activate in your agent's runtime. Use `--venv <path>` to target the right venv, `--all` to sweep every venv, or run `toolrecall setup`.
- **Healthcheck indicator:** `toolrecall stats`/healthcheck shows `shim=active|inactive`
  (derived from `shim --status --all`, `probe: pass` = active). `shim=active` with
  nonzero `file_cache` = caching working; `mcp_cache=0` alongside is expected
  (shim bypasses the MCP bridge). `shim=inactive` → install with `--venv`/`--all`.
  See `docs/TROUBLESHOOTING.md` §16.

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

### `toolrecall stop`

- **File:** `cli.py : cmd_stop()`
- **Purpose:** Stop the daemon and **revert the forward-proxy base-URL wiring** agents
  were pointed at, so they call their providers directly again.
- **Note:** The forward proxy (`:8569`) is owned by the daemon process and stops with
  it — stopping the daemon automatically takes the proxy down.
- **Revert scope:** Strips only the exact `localhost`/`127.0.0.1:8569` override lines from
  `~/.bashrc`, `~/.profile`, and `~/.hermes/config.yaml` (each touched file is backed up
  with a timestamped sibling). Real-host overrides and unrelated lines are left intact.
- **Restart with:** `toolrecall daemon`

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

### `toolrecall healthcheck`

- **File:** `cli.py : cmd_healthcheck()` → `toolrecall/healthcheck.py`
- **Purpose:** One-shot daemon/cache health status for operators and cron
  watchdogs. Reuses ToolRecall's **own** transport/pid/lock paths — no
  hardcoded per-machine config.
- **What it reports:** daemon process count, `daemon.pid` liveness, `daemon-*.lck`
  lock-file count, UDS socket presence, shim state (`active`/`inactive`), and
  cache hit-rate.
- **Exit codes:**
  - `0` — healthy
  - `1` — daemon down or abnormal pid/lock/socket state (warns, with `note:` line)
  - `2` — hard error
- **Cron usage:** `TOOLRECALL_BIN=... toolrecall healthcheck` (cron shells don't
  inherit `~/.local/bin` PATH). The Hermes watchdog wraps this command and adds
  category-level zero-hits interpretation; the core signal lives here.

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