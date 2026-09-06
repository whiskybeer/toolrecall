# Configuration Reference — `config.toml`, `config.py`, and Environment Variables

ToolRecall uses a single configuration file (`config.toml`) loaded by `toolrecall/config.py`.  
All settings have defaults — you only need to override what you want to change.

## File: `toolrecall/config.toml`

The package-default config shipped with ToolRecall. Located at `toolrecall/config.toml` in the package directory. Users can override it via `~/.config/toolrecall/toolrecall.toml` or `toolrecall init`.

### Sections

| Section | Purpose | Key Options |
|---------|---------|-------------|
| `[paths]` | Data file locations | `cache_db`, `knowledge_db`, `skill_dirs` |
| `[storage]` | Backend engine + Turso sync | `backend`, `libsql_db`, `sync_enabled`, `sync_url`, `sync_token`, `sync_interval`, `turso_api_base` | `sqlite`, `libsql`, or `libsql-sync` |
| `[cache]` | Cache TTLs and hashing | `file_ttl`, `terminal_default_ttl`, `hash_algorithm`, `log_shell_fallback`, `stale_while_revalidate`, `adaptive_ttl`, `adaptive_factor`, `adaptive_max_ttl`, `fuzzy_ttl_match`, `fuzzy_threshold` |
| `[cache.terminal_ttls]` | Per-command TTL overrides | Any command as key, TTL in seconds as value |
| `[cache.file_ttls]` | Per-path TTL overrides (files, folders, file types) | Glob or exact path as key, TTL in seconds as value |
| `[norm]` | Semantic key normalization | `enabled`, `sort_lists`, `strip_strings`, `canonical_commands` |
| `[resilience]` | Coalescing / retry / circuit breaker (all opt-in) | `coalescing`, `coalescing_window`, `retry`, `retry_max_attempts`, `retry_backoff_base`, `circuit_breaker`, `cb_failure_threshold`, `cb_window`, `cb_open_seconds` |
| `[nginx]` | Optional nginx config generation | `site_name`, `domain`, `ssl` |
| `[security]` | MCP security gates | `tool_access_control`, `dangerous_tool_keywords`, `cognitive_check_enabled`, `ast_check_enabled` |
| `[mcp]` | MCP file/terminal access | `allowed_paths`, `allow_terminal`, `allow_invalidate`, `emit_context_hints`, `allowed_terminal_commands` |
| `[mcp_multiplex]` | MCP Multiplexer settings | `enabled`, `servers`, `servers_config`, `idle_minutes`, `default_ttl`, `transparent_cache` |
| `[mcp_multiplex.servers_config]` | Custom server overrides | Any server name with `command`, `args`, `env`, `ttl` |
| `[sources]` | Knowledge DB scanning | `scan_dirs` (default: `~/.hermes/memories` + `~/.hermes/skills`), `scan_extensions`, `scan_ignore`, `max_file_kb` |
| `[sources.memory]` | Agent memory indexing | `enabled` |
| `[docs]` | Knowledge DB (FTS) index freshness | `index_ttl` (seconds, default `86400`, `0` = never auto-refresh; env `TOOLRECALL_DOCS_INDEX_TTL`) |
| `[recall]` | Recall Tier (lossless-recoverable eviction) | `enabled` (opt-in, default `false`), `summarizer` (Phase 2) |
| `[forward_proxy]` | Forward proxy port | Port via `TOOLRECALL_FORWARD_PORT` env var |

### Config Loading Priority

```
Environment variables (TOOLRECALL_*)  ← highest priority
CWD: toolrecall.toml
~/.config/toolrecall/toolrecall.toml
/etc/toolrecall/toolrecall.toml
Package default: config.toml          ← base defaults
```

## File: `toolrecall/config.py`

The Python configuration loader (`Config` class). Responsibilities:

- **Load chain:** Merges user configs on top of package defaults, deepest section wins.
- **Env override:** `TOOLRECALL_*` environment variables override any TOML value (see table below).
- **Path expansion:** `~`, `$HOME`, `$VAR` in paths are expanded to absolute paths.
- **MCP Auto-Resolution:** `mcp_multiplex_servers_config` property resolves server names via the registry (`toolrecall/mcp_registry.py`), falling back to explicit `servers_config` overrides.
- **Agent home resolution:** `agent_home` property checks `AGENT_HOME` → `TOOLRECALL_AGENT_HOME` → config → `~/.hermes`.
- **Skill dirs:** `skill_dirs` resolves from env var → config → `agent_home/skills`.

### Key Properties

| Property | Purpose | Default |
|----------|---------|---------|
| `cache_db` | Path to cache SQLite DB | `~/.toolrecall/cache.db` |
| `knowledge_db` | Path to FTS5 knowledge DB | `~/.toolrecall/knowledge.db` |
| `agent_home` | Agent's home directory | `~/.hermes` |
| `skill_dirs` | Where to search for skills | `agent_home/skills` |
| `storage_backend` | Cache backend engine | `sqlite` | `libsql` for multi-writer/vector search; `libsql-sync` for Turso Cloud sync via pyturso |
| `libsql_db` | libSQL DB path (defaults to `~/.toolrecall/cache-libsql.db` — separate from sqlite3) | `None` | `~/.toolrecall/cache-libsql.db` |
| `sync_enabled` | Master switch for Turso sync (opt-in, default false) | `False` | Must be explicitly `true` for sync to run |
| `sync_url` | Turso Cloud sync URL | `None` | `libsql://my-db.turso.io` |
| `sync_token` | Turso Cloud auth token | `None` | Turso API token |
| `sync_interval` | Sync interval in seconds (`0` = disabled) | `60` | `300` |
| `turso_api_base` | Turso Platform API base URL | `https://api.turso.tech` | Customizable for self-hosted/proxy |
| `file_ttl` | File cache TTL (seconds) | `-1` (infinite) |
| `file_ttls` | Per-path TTL overrides (globs/exact); see [Per-Path File TTLs](#per-path-file-ttls) below | `{}` |
| `terminal_default_ttl` | Default terminal TTL | `300` (5 min) |
| `mcp_allowed_paths` | Default-deny read allowlist | `[]` |
| `mcp_allow_terminal` | Allow shell execution | `true` |
| `mcp_allow_invalidate` | Allow cache invalidation | `false` |
| `mcp_emit_context_hints` | Append 🧹 drop-clean hints after tool calls | `true` |
| `mcp_client_hint_policy` | Per-client `[mcp.clients]` overrides of `emit_context_hints`, keyed by MCP `clientInfo.name` | `{}` |
| `mcp_multiplex_servers` | Server names to multiplex | `[]` |
| `mcp_multiplex_servers_config` | Resolved server configs | Registry + overrides |

## Environment Variables (`TOOLRECALL_*`)

All `TOOLRECALL_*` env vars override their corresponding config.toml key. List values can be comma-separated.

| Env Variable | Config Key | Example |
|-------------|------------|---------|
| `TOOLRECALL_CACHE_DB` | `paths.cache_db` | `TOOLRECALL_CACHE_DB=/tmp/my-cache.db` |
| `TOOLRECALL_KNOWLEDGE_DB` | `paths.knowledge_db` | |
| `TOOLRECALL_SKILL_DIRS` | `paths.skill_dirs` | `TOOLRECALL_SKILL_DIRS=~/.hermes/skills,~/.custom/skills` |
| `TOOLRECALL_FILE_TTL` | `cache.file_ttl` | `TOOLRECALL_FILE_TTL=3600` |
| `TOOLRECALL_TERMINAL_TTL` | `cache.terminal_default_ttl` | |
| `TOOLRECALL_SCAN_DIRS` | `sources.scan_dirs` | |
| `TOOLRECALL_NGINX_DOMAIN` | `nginx.domain` | |
| `TOOLRECALL_MCP_ALLOWED_PATHS` | `mcp.allowed_paths` | `TOOLRECALL_MCP_ALLOWED_PATHS=/home/user/projects` |
| `TOOLRECALL_MCP_ALLOW_TERMINAL` | `mcp.allow_terminal` | `TOOLRECALL_MCP_ALLOW_TERMINAL=true` |
| `TOOLRECALL_MCP_ALLOW_INVALIDATE` | `mcp.allow_invalidate` | |
| `TOOLRECALL_MCP_EMIT_CONTEXT_HINTS` | `mcp.emit_context_hints` | `TOOLRECALL_MCP_EMIT_CONTEXT_HINTS=false` |
| `TOOLRECALL_MCP_MULTIPLEX_ENABLED` | `mcp_multiplex.enabled` | |
| `TOOLRECALL_MCP_MULTIPLEX_SERVERS` | `mcp_multiplex.servers` | `TOOLRECALL_MCP_MULTIPLEX_SERVERS=time,github` |
| `TOOLRECALL_MCP_MULTIPLEX_TRANSPARENT_CACHE` | `mcp_multiplex.transparent_cache` | |
| `TOOLRECALL_MCP_MULTIPLEX_DEFAULT_TTL` | `mcp_multiplex.default_ttl` | |
| `TOOLRECALL_STORAGE_BACKEND` | `storage.backend` | `TOOLRECALL_STORAGE_BACKEND=libsql` or `TOOLRECALL_STORAGE_BACKEND=libsql-sync` |
| `TOOLRECALL_LIBSQL_DB_PATH` | `storage.libsql_db` | `TOOLRECALL_LIBSQL_DB_PATH=~/.toolrecall/cache-libsql.db` |
| `TOOLRECALL_SYNC_ENABLED` | `storage.sync_enabled` | `TOOLRECALL_SYNC_ENABLED=true` |
| `TOOLRECALL_SYNC_URL` | `storage.sync_url` | `TOOLRECALL_SYNC_URL=libsql://my-db.turso.io` |
| `TOOLRECALL_SYNC_TOKEN` | `storage.sync_token` | |
| `TOOLRECALL_SYNC_INTERVAL` | `storage.sync_interval` | `TOOLRECALL_SYNC_INTERVAL=300` |
| `TOOLRECALL_TURSO_API_BASE` | `storage.turso_api_base` | `TOOLRECALL_TURSO_API_BASE=https://turso.internal.example` |
| `TOOLRECALL_HASH_ALGORITHM` | `cache.hash_algorithm` | `TOOLRECALL_HASH_ALGORITHM=sha256` |
| `TOOLRECALL_LOG_SHELL_FALLBACK` | `cache.log_shell_fallback` | |
| `TOOLRECALL_RECALL_ENABLED` | `recall.enabled` | `TOOLRECALL_RECALL_ENABLED=true` |
| `TOOLRECALL_FORWARD_PORT` | (not in config.toml) | `TOOLRECALL_FORWARD_PORT=9090` |
| `TOOLRECALL_FETCH_MAX_BYTES` | (not in config.toml) | `TOOLRECALL_FETCH_MAX_BYTES=1048576` (1MB) |
| `TOOLRECALL_FETCH_LOG` | (not in config.toml) | `TOOLRECALL_FETCH_LOG=~/.toolrecall/fetch_api.log` |
| `TOOLRECALL_UDS_PATH` | (not in config.toml) | `TOOLRECALL_UDS_PATH=/tmp/tc.sock` |
| `TOOLRECALL_TRANSPORT` | (not in config.toml) | `TOOLRECALL_TRANSPORT=tcp` (Windows fallback) |
| `TOOLRECALL_SHIM_DISABLE` | (not in config.toml) | `TOOLRECALL_SHIM_DISABLE=1` (disable OS-level shim per-process) |
| `AGENT_HOME` | `paths.agent_home` (resolution) | `AGENT_HOME=~/.hermes` |
| `TOOLRECALL_AGENT_HOME` | `paths.agent_home` (resolution) | |

## Per-client context hints (`[mcp.clients]`)

Mixed-agent daemons (e.g. Warp hosting Claude Code alongside Hermes) can
silence context-hint emission for specific clients by MCP `clientInfo.name`
while keeping the global default for everyone else:

```toml
[mcp]
emit_context_hints = true        # global default (stateless agents: Hermes, OpenCode, Cline)

[mcp.clients."claude-code"]
emit_context_hints = false        # context-managing agent: no hints

[mcp.clients."warp-agent"]
emit_context_hints = false
```

Clients not listed fall back to `[mcp].emit_context_hints`. Additionally,
hints are only emitted **after** a client calls `context_set_checkpoint`
once — the bridge stays passive until an agent demonstrates the checkpoint
pattern, so context-managing agents never see hint text even without config.

## Per-Path File TTLs

`[cache.file_ttls]` overrides the global `file_ttl` for specific files,
folders, or file types. Keys are fnmatch globs (or exact paths, `~` works)
matched against the absolute path; values are TTL seconds.

```toml
[cache.file_ttls]
"logs/app.log" = 0            # actively appended: churns, don't cache
"**/*.log.[0-9]*" = 86400     # rotated logs: immutable — window only skips stat()
"~/vendor/**" = 3600          # build-touched vendored deps: skip identical re-reads
"package-lock.json" = 86400   # exact file: trust for a day
```

Value semantics:

| Value | Meaning |
|-------|---------|
| `-1` | Always re-validate mtime before serving (default; correct, one `stat()` per read). Already optimal for **immutable files** — rotated logs, archives: the check passes, content comes from cache, zero staleness risk. |
| `0` | Never cache — every read goes to disk; nothing enters memory LRU or SQLite. For files that churn faster than they are read (actively-appended logs). |
| `>0` | **Trust window**: for N seconds after caching, serve the cached copy without checking disk. Pays off when the mtime changes *without* the content changing (build systems rewriting lockfiles, vendored deps, generated files) — it skips the pointless re-read of identical content. |

Precedence per read: exact path → first glob match → `[cache].file_ttl`.

**Correctness trade-off:** the default mtime validation is what makes cached
reads safe — a file edited on disk is re-read immediately. A `>0` trust
window deliberately skips that check: a file edited *inside* the window
serves stale content until the window expires. Only opt in for paths that
change rarely (notes, vendored dependencies, generated reference docs).
Changes require a daemon restart to take effect.

## Stale-While-Revalidate, Adaptive TTL & Resilience

All of the following are **opt-in**; every default preserves the classic
hard-TTL behavior exactly.

### Stale-While-Revalidate (`[cache].stale_while_revalidate`)

Serve a cache entry up to N seconds past its expiry, flagged `"stale": true`
in the response, then refresh it:

| Layer | Refresh mechanism |
|-------|-------------------|
| `terminal_cache` | Background thread (single-flight per key); refreshed row keeps its hit streak if content is unchanged |
| `mcp_cache` | `cached_mcp()` refetches synchronously via the caller's `fetch_fn`; `cached_mcp_check()` returns `"stale": true` and the caller decides |
| `api_cache` (proxy) | **Serve-stale only — an LLM completion is never auto-replayed** (billing guard); the next fresh pass refreshes the row |

Entries older than the SWR window are ordinary misses.

### Adaptive TTL (`[cache].adaptive_ttl`)

Each fresh cache hit increments the entry's `hit_streak`. On re-store:
`ttl_eff = min(base_ttl * adaptive_factor ** hit_streak, adaptive_max_ttl)`.
The streak resets whenever fresh content differs from the cached content —
stable outputs (e.g. `git branch`) stretch toward the cap, volatile ones
stay at base TTL. SWR serves do not count as hits.

### Fuzzy TTL classification (`[cache].fuzzy_ttl_match`)

A terminal command matching **no** exact/prefix pattern in
`DEFAULT_CACHEABLE`/`[cache.terminal_ttls]` inherits the TTL of the most
similar pattern (difflib ratio ≥ `fuzzy_threshold`, default `0.85`).
Classification only — serving always happens under the command's own hash
key, so cross-key poisoning is impossible.

### Canonical command keys (`[norm].canonical_commands`)

Switches terminal cache-key generation to `canonical_command()`: flag
clusters sort with combined-short-flag decomposition (`ls -al` ≡ `ls -la` ≡
`ls -a -l`), `~` expands, duplicate slashes and trailing slashes collapse,
redundant quotes drop. Enabling it after running with it off invalidates
existing terminal entries once (one re-warm).

### Resilience (`[resilience]`)

| Key | Default | Behavior |
|-----|---------|----------|
| `coalescing` | `false` | Concurrent identical misses share **one** execution (terminal subprocess, MCP fetch, proxy forward). Waiters get the winner's outcome; a waiter blocked longer than `coalescing_window` s executes alone. |
| `retry` | `false` | Proxy `_forward` only: connection errors and HTTP 429/502/503/504 retried up to `retry_max_attempts` with exponential backoff + full jitter (`retry_backoff_base`); `Retry-After` honored (≤5 s). **A response is never retried after its body was consumed** (billing guard); streaming is never retried. |
| `circuit_breaker` | `false` | Per-process breaker around proxy `_forward`: `cb_failure_threshold` 429/5xx within `cb_window` s → OPEN (fast-fail `503` + `Retry-After` + `{"error":{"code":"circuit_open"}}`, no upstream contact); after `cb_open_seconds` one probe is admitted — success closes, failure reopens. |

Env overrides: `TOOLRECALL_STALE_WHILE_REVALIDATE`, `TOOLRECALL_ADAPTIVE_TTL`,
`TOOLRECALL_ADAPTIVE_FACTOR`, `TOOLRECALL_ADAPTIVE_MAX_TTL`,
`TOOLRECALL_FUZZY_TTL_MATCH`, `TOOLRECALL_FUZZY_THRESHOLD`,
`TOOLRECALL_CANONICAL_COMMANDS`, `TOOLRECALL_RESILIENCE_COALESCING`,
`TOOLRECALL_COALESCING_WINDOW`, `TOOLRECALL_RETRY`,
`TOOLRECALL_RETRY_MAX_ATTEMPTS`, `TOOLRECALL_RETRY_BACKOFF_BASE`,
`TOOLRECALL_CIRCUIT_BREAKER`, `TOOLRECALL_CB_FAILURE_THRESHOLD`,
`TOOLRECALL_CB_WINDOW`, `TOOLRECALL_CB_OPEN_SECONDS`.

## See Also

- [MCP Multiplexer](MCP_MULTIPLEXER.md) — server registry, auto-resolution, `servers_config`
- [Security Architecture](../SECURITY.md) — `allowed_paths`, `tool_access_control`, cognitive scan
- [libSQL Backend](LIBSQL_COMPARISON.md) — backend comparison, opt-in cloud sync, security implications
- [Hermes Transparent Cache](HERMES_TRANSPARENT_CACHE.md) — OS-level .pth shim details