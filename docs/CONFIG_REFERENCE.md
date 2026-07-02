# Configuration Reference — `config.toml`, `config.py`, and Environment Variables

ToolRecall uses a single configuration file (`config.toml`) loaded by `toolrecall/config.py`.  
All settings have defaults — you only need to override what you want to change.

## File: `toolrecall/config.toml`

The package-default config shipped with ToolRecall. Located at `toolrecall/config.toml` in the package directory. Users can override it via `~/.toolrecall/config.toml` or `toolrecall init`.

### Sections

| Section | Purpose | Key Options |
|---------|---------|-------------|
| `[paths]` | Data file locations | `cache_db`, `knowledge_db`, `skill_dirs` |
| `[storage]` | Backend engine | `backend` (`sqlite` default) |
| `[cache]` | Cache TTLs and hashing | `file_ttl`, `terminal_default_ttl`, `hash_algorithm`, `log_shell_fallback` |
| `[cache.terminal_ttls]` | Per-command TTL overrides | Any command as key, TTL in seconds as value |
| `[nginx]` | Optional nginx config generation | `site_name`, `domain`, `ssl` |
| `[security]` | MCP security gates | `tool_access_control`, `dangerous_tool_keywords`, `cognitive_check_enabled`, `ast_check_enabled` |
| `[mcp]` | MCP file/terminal access | `allowed_paths`, `allow_terminal`, `allow_invalidate`, `allowed_terminal_commands` |
| `[mcp_multiplex]` | MCP Multiplexer settings | `enabled`, `servers`, `servers_config`, `idle_minutes`, `default_ttl`, `transparent_cache` |
| `[mcp_multiplex.servers_config]` | Custom server overrides | Any server name with `command`, `args`, `env`, `ttl` |
| `[hermes]` | Hermes Agent integration | `transparent_cache` (`separate` or `transparent`) |
| `[sources]` | Knowledge DB scanning | `scan_dirs`, `scan_extensions`, `scan_ignore`, `max_file_kb` |
| `[sources.memory]` | Agent memory indexing | `enabled` |
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
| `storage_backend` | Cache backend engine | `sqlite` |
| `file_ttl` | File cache TTL (seconds) | `-1` (infinite) |
| `terminal_default_ttl` | Default terminal TTL | `300` (5 min) |
| `mcp_allowed_paths` | Default-deny read allowlist | `[]` |
| `mcp_allow_terminal` | Allow shell execution | `false` |
| `mcp_allow_invalidate` | Allow cache invalidation | `false` |
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
| `TOOLRECALL_MCP_MULTIPLEX_ENABLED` | `mcp_multiplex.enabled` | |
| `TOOLRECALL_MCP_MULTIPLEX_SERVERS` | `mcp_multiplex.servers` | `TOOLRECALL_MCP_MULTIPLEX_SERVERS=time,github` |
| `TOOLRECALL_MCP_MULTIPLEX_TRANSPARENT_CACHE` | `mcp_multiplex.transparent_cache` | |
| `TOOLRECALL_MCP_MULTIPLEX_DEFAULT_TTL` | `mcp_multiplex.default_ttl` | |
| `TOOLRECALL_STORAGE_BACKEND` | `storage.backend` | |
| `TOOLRECALL_HASH_ALGORITHM` | `cache.hash_algorithm` | `TOOLRECALL_HASH_ALGORITHM=sha256` |
| `TOOLRECALL_LOG_SHELL_FALLBACK` | `cache.log_shell_fallback` | |
| `TOOLRECALL_FORWARD_PORT` | (not in config.toml) | `TOOLRECALL_FORWARD_PORT=9090` |
| `TOOLRECALL_FETCH_MAX_BYTES` | (not in config.toml) | `TOOLRECALL_FETCH_MAX_BYTES=1048576` (1MB) |
| `TOOLRECALL_FETCH_LOG` | (not in config.toml) | `TOOLRECALL_FETCH_LOG=~/.toolrecall/fetch_api.log` |
| `TOOLRECALL_UDS_PATH` | (not in config.toml) | `TOOLRECALL_UDS_PATH=/tmp/tc.sock` |
| `TOOLRECALL_TRANSPORT` | (not in config.toml) | `TOOLRECALL_TRANSPORT=tcp` (Windows fallback) |
| `TOOLRECALL_SHIM_DISABLE` | (not in config.toml) | `TOOLRECALL_SHIM_DISABLE=1` (disable OS-level shim per-process) |
| `AGENT_HOME` | `paths.agent_home` (resolution) | `AGENT_HOME=~/.hermes` |
| `TOOLRECALL_AGENT_HOME` | `paths.agent_home` (resolution) | |

## See Also

- [MCP Multiplexer](MCP_MULTIPLEXER.md) — server registry, auto-resolution, `servers_config`
- [Security Architecture](../SECURITY.md) — `allowed_paths`, `tool_access_control`, cognitive scan
- [Hermes Transparent Cache](HERMES_TRANSPARENT_CACHE.md) — `[hermes]` section details