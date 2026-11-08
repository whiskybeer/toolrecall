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
| `toolrecall trend` | `cmd_trend()` | `toolrecall/cli.py` |
| `toolrecall invalidate` | `cmd_invalidate()` | `toolrecall/cli.py` |
| `toolrecall reset-stats` | `cmd_reset_stats()` | `toolrecall/cli.py` |
| `toolrecall dedup-stats` | `cmd_dedup_stats()` | `toolrecall/cli.py` |
| `toolrecall index` | `cmd_index()` | `toolrecall/cli.py` |
| `toolrecall index-compact` | `cmd_index_compact()` | `toolrecall/cli.py` |
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
| `toolrecall replay` | `cmd_replay()` → dispatches subcommands | `toolrecall/replay_cli.py` |
| `toolrecall turso` | `cmd_turso()` → dispatches subcommands | `toolrecall/turso_cli.py` |
| `toolrecall agent` | `cmd_agent()` → dispatches subcommands | `toolrecall/cli.py` |
| `toolrecall context` | `cmd_context()` → dispatches subcommands | `toolrecall/cli.py` |
| `toolrecall logs` | `cmd_logs()` | `toolrecall/cli.py` |
| `toolrecall update` | `cmd_update()` | `toolrecall/cli.py` |
| `toolrecall top` | `cmd_top()` → `toolrecall/tui_top.py` | `toolrecall/cli.py` |
| `toolrecall proxy` | `cmd_proxy()` → dispatches subcommands | `toolrecall/cli.py` |
| `toolrecall healthcheck` | `cmd_healthcheck()` | `toolrecall/cli.py` |
| `toolrecall hook` | `cmd_hook()` → `toolrecall/adapters/claude_code.py` | `toolrecall/cli.py` |
| `toolrecall audit` | `cmd_audit()` → `toolrecall/adapters/claude_code_audit.py` | `toolrecall/cli.py` |
| `toolrecall keytrace` | `cmd_keytrace()` → `toolrecall/keytrace.py` | `toolrecall/cli.py` |

Each function imports its dependencies lazily — running `toolrecall status` does not load `daemon.py` or `proxy.py`.

Most commands accept `--help` / `-h` for per-command usage and options
(`toolrecall trend --help`, `toolrecall mcp --help`, `toolrecall update --help`,
`toolrecall shim` with unknown flags, ...). `toolrecall --help` lists all commands.

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
- **Flags:**
  - `--by-agent`: Print human-readable per-agent breakdown from `agent_cache_stats`.
  - `--agent-id <id>`: Filter per-agent breakdown to a specific agent identity.

### `toolrecall agent`

- **File:** `cli.py : cmd_agent()`
- **Purpose:** Manage per-agent cache statistics (see `docs/AGENT_TRACKING.md`).
- **Subcommands:**
  - `toolrecall agent ls [--json]`: List all known agents with hit/miss and token counts.
    `--json` prints a JSON array (one object per agent; `[]` when nothing is recorded).
  - `toolrecall agent show <id> [--json]`: Detailed breakdown per category for an agent.
    `--json` prints one object `{agent_id, categories, totals}`; an unknown agent yields
    empty `categories` and zero `totals`.
  - `toolrecall agent prune <id>`: Delete stats rows for an agent (preserves cached content).

### `toolrecall trend`

- **File:** `cli.py : cmd_trend()`, aggregation in `toolrecall/trend.py`
- **Purpose:** Daily hit-rate trend + worst miss-heavy path classes, computed from the `access_log` rolling window (~50k entries). Lets users verify how well the cache serves *their* workload over time and identify never-cacheable classes (rewritten-on-every-access files) that cap the headline rate. See README section "Verifying Your Hit Rate".
- **Flags:** `--days N` (default 7), `--json` (machine-readable: `{"trend", "miss_classes", "coverage"}`).
- **No daemon:** opens the cache DB read-only (WAL-safe); works even when the daemon is down. Not in `_DAEMON_REQUIRED`.
- **Stateless:** no rollup tables or background aggregation — window retention is the retention policy.

### `toolrecall invalidate`

- **File:** `cli.py : cmd_invalidate()`
- **Purpose:** Clear agent caches (file, terminal, MCP, script) — api_cache (billed LLM responses) is deliberately kept; pass scope="api" to include it. Uses daemon if running, else direct SQLite.

### `toolrecall reset-stats`

- **File:** `cli.py : cmd_reset_stats()`
- **Purpose:** Reset the statistics *window* (hit/miss/token counters and the
  `access_log` trend window). Both are **moved into banked lifetime totals**
  (`cache_stats_banked`, `access_log_banked`) instead of being deleted, so
  `toolrecall stats` keeps its `lifetime_*` values and `toolrecall trend` still
  reaches back past the reset. Cache entries are never touched. Useful for benchmarking.

### `toolrecall dedup-stats`

- **File:** `cli.py : cmd_dedup_stats()`, ledger in `toolrecall/dedup_stats.py`
- **Purpose:** Prints a per-tool breakdown of request-body deduplication savings.
  While the proxy usage log records aggregate `dedup_tokens` per request,
  `dedup-stats` attributes stubbed duplicates to the specific tool that produced
  them (e.g. `skills_list`, `read_file`, `git_status`) via the OpenAI `tool_call_id`
  or `name` field.
- **Output:** Columns for `tool`, `stubs` (duplicate blocks stubbed), `tokens saved`
  (estimated as `chars // 4` matching `proxy_usage.csv`), `share` percentage,
  `requests` affected, and timestamp of the `last` stub event.
- **Fail-safe:** Reads from the lazy `dedup_by_tool` table. If no duplicate blocks
  have been stubbed yet, explains that dedup only stubs identical blocks (≥800 chars)
  and notes that an empty table is not proof of breakage.

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
    `.pth` present, `probe: pass` (verified) or `probe: FAIL`, the
    venv-vs-install version pair, and `shim modules: in sync` / `DRIFT`
  - **Drift** = that venv's shim modules differ in *content* from the running
    install, so it still runs older shim logic even when the version strings
    agree. `probe: pass` cannot see it — a stale module still imports fine.
    `toolrecall shim --install --venv <path>` converges the venv (from the
    index, or by overlaying the live source tree when this install is a
    checkout). See `docs/TROUBLESHOOTING.md` §19.
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
  (Hermes, OpenCode, Antigravity, Claude Code, Cline, Cursor) and wires up the MCP
  bridge automatically (using multiplexer-only profiles for Claude Code and Cursor).
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
  - `toolrecall replay tree [<tree_id>]` — list fork trees, or show one tree's nodes
  - `toolrecall replay tree new <name>` — create a fork tree (prints tree_id + root node)
  - `toolrecall replay fork <tree> <node> <name>` — fork a branch from a node
  - `toolrecall replay at <tree> <node>` — show the call chain a node inherits (root first)
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
- **What it reports:** daemon process count, `daemon.pid` liveness (live,
  non-zombie PID), `daemon-*.lck` lock-file count, UDS socket presence, shim
  state (`active`/`inactive`), and cache hit-rate.
- **Flags:**
  - `--json` — one JSON object: `{"health": {...}, "warnings": [...]}`, plus
    `"cleanup": {...}` when `--clean-stale` is given.
  - `--clean-stale` — remove a *provably* dead `daemon.pid` and orphaned
    `daemon-*.lck` files before reporting. A PID file is kept while a daemon
    holds the instance lock (running or starting); a lock file is removed only
    when its `flock` can be taken, so a live holder is never touched. See
    [Troubleshooting §13](TROUBLESHOOTING.md) for the crash recovery recipe.
- **Side effects:** none. Helper subprocesses run with
  `TOOLRECALL_NO_AUTOSTART=1`, so a healthcheck never starts the daemon it is
  diagnosing (it used to, which rewrote `daemon.pid` mid-check).
- **Exit codes:**
  - `0` — daemon process present (warnings such as a stale pid or a noisy log
    may still be printed on the `note:` line)
  - `1` — daemon down (no `toolrecall daemon` process)
  - `2` — hard error or unknown flag
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

### `toolrecall hook claude`

- **File:** `cli.py : cmd_hook()` → `adapters/claude_code.py : main()`
- **Purpose:** Claude Code lifecycle hooks and setup snippets.
  - `toolrecall hook claude print-config [command]` — `settings.json` hooks block
    (staleness alert, optional WAF gate). Without a subcommand the adapter reads
    one hook event as JSON on stdin.
  - `toolrecall hook claude ci-env [--port N]` — shell snippet for repeated headless
    runs through the forward proxy (canonicalize header, `PYTHONHASHSEED=0`, pinned MCP).
- See [CLAUDE_CODE_INTEGRATIONS.md](CLAUDE_CODE_INTEGRATIONS.md).

### `toolrecall audit claude-code`

- **File:** `cli.py : cmd_audit()` → `adapters/claude_code_audit.py : main()`
- **Purpose:** read-only report of where Claude Code session tokens go: exact fixed
  starting context per session, its share of list-price-weighted cost, and the
  controllable parts (instruction files, skill/MCP/agent listings) ranked by cost share.
- **Options:** `--days N`, `--projects DIR` (default `~/.claude/projects`),
  `--include-tmp` (sessions whose workdir is under `/tmp`), `--json`.

### `toolrecall keytrace`

- **File:** `cli.py : cmd_keytrace()` → `keytrace.py : main()`
- **Purpose:** explain forward-proxy replay misses. Start the proxy with
  `TOOLRECALL_KEY_TRACE_DIR=<dir>`, run the workload, then `toolrecall keytrace <dir>
  [--json]`: each MISS shows the first field that differs from the closest earlier
  request (or "first request" / "new step" / "identical body seen before").

### `toolrecall proxy`

- **File:** `cli.py : cmd_proxy()` → `_attach_hermes_to_proxy()` / `_detach_hermes_from_proxy()`
- **Purpose:** Point Hermes' LLM traffic at the ToolRecall forward proxy —
  the billed-savings path (`api_cache` replay + provider usage capture,
  i.e. the MONEY metric tier).
- **Subcommands:**
  - `toolrecall proxy attach` — point `~/.hermes/config.yaml`
    `llm.base_url` → `http://127.0.0.1:8569/v1` (port via
    `TOOLRECALL_FORWARD_PORT`). Updates the line in place, or **inserts** it
    as the section's first key when the LLM section carries none (using that
    section's own child indentation). Creates `config.yaml.toolrecall-bak` on
    first attach. Idempotent (no-op when already proxied).
  - `toolrecall proxy detach` — restore the backup, removing the proxy
    override.
- **Safety gates:**
  - Health check: refuses to attach unless the proxy answers on its port
    (any HTTP response — including upstream 401 — proves it's alive;
    connection errors refuse). Never points an agent at a dead proxy.
  - A config with no `model:`/`llm:` section is refused rather than guessed
    at: attach names the manual `OPENAI_BASE_URL` route instead of writing a
    section shape it cannot verify.
  - Opt-in: `setup` asks interactively (or with `--yes`); non-interactive
    runs without `--yes` skip it. Attaching changes the agent's LLM
    failure mode — must never be silent.
  - Reversible: `detach` restores the pre-attach config byte-exact.
- **Setup integration:** `toolrecall setup` runs this as step 5 when
  Hermes is detected (same gates).
- Note: after attach, restart Hermes to route traffic through the proxy.

### `toolrecall top`

- **File:** `cli.py : cmd_top()` → `toolrecall/tui_top.py`
- **Purpose:** Interactive TUI dashboard (stdlib `curses`, zero deps).
  Read-only by design: the only write actions are the safe ones (refresh
  a stale file, set a context checkpoint). No invalidate/reset keys.
- **Views** (`1`–`5` or `Tab` to cycle, `q` quit, lone `ESC` quits):
  - `1` **saved** — billed truth + measured not-sent (the end-user view):
    `sent to LLM today` vs the umbrella `not sent (measured)` total —
    stubbed re-reads (unchanged file re-reads replaced by one-line stubs,
    today-scoped from `access_log`) + deduped blocks (byte-identical
    blocks removed within a request, from the proxy usage CSV). Both
    mechanisms are wire-verified and disjoint (stubbed content never
    reaches the proxy), so the sum is exact; the percentage uses the
    pre-removal base (`removed / (billed + removed)`). Below it: billed
    prompt/completion, replays, the provider prefix-cache line (`X tok
 re-served by provider — p% of sent [· discount $Y]` — bill-side
 discount on sent tokens, outside the umbrella; dollars only when the
 provider reports them), provider-reported cost, the median-request
 diagnostic, the cumulative stub ledger (explicitly "logged days" —
 `access_log` is a rolling window), and blocked reads. The REPLAY
 tier below it reconstructs the naive-world equivalent for the same
 sessions (15-min gap rule, stubs/dedup switched off, 200K wall) —
 inputs measured, counterfactual constructed.
  - `2` **log** — scrollable access-log tail (`↑`/`↓`, `PgUp`/`PgDn`)
  - `3` **trend** — daily file_cache hit-rate from `access_log` (7-day view
    with bar visual) + worst hit-rate path classes (the plateau diagnostic,
    from `toolrecall/trend.py`; daemon-independent, reads the db directly)
  - `4` **stats** — per-cache IO hits/misses/hit-rate/token accounting
    (latency tier: content reaches the LLM either way — not money)
  - `5` **help** — keybindings + the metric-tier vocabulary (IO / CONTEXT /
    MONEY — don't conflate)
- **Wrong-user guard:** if the current user has no cache data but another
  user's `~/.toolrecall/cache.db` exists on the box (daemon runs as that
  user), the stats view says so and prints the `sudo -u` escape instead of
  a bare empty table — daemon state is per-user.
- **Actions** (require the daemon): `Enter` on a stale file refreshes it
  (`cache_refresh_file` — invalidate + fresh read, clears staleness);
  `c` sets a new context-tracker checkpoint. A footer toast confirms each.
- **Data source:** daemon UDS when reachable; automatic read-only SQLite
  fallback for stats/log when the daemon is down — works on any machine,
  hosted or not. Context tracker needs the daemon.
- **Usage:** `toolrecall top` (interactive; auto plain-text snapshot when
  stdout is not a tty) · `toolrecall top --once` (single snapshot).
- **Options:** `--interval SECONDS` (default 2.0), `--db PATH` (cache.db
  override), `--once`.

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