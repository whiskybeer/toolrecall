# Changelog

## v0.5.4-dev

- **Tests:** 275 tests, all passing (was 176). Added coverage for:
  - `test_transport.py` — UDS/TCP IPC, socket lifecycle, framed protocol (transport.py)
  - `test_mcp_bridge.py` — MCP JSON-RPC protocol, security gate filtering (mcp_bridge.py)
  - `test_client.py` — daemon-first routing with direct SQLite fallback (client.py)
  - `test_cli.py` — CLI dispatch for all 16 subcommands (cli.py)
- **Fix:** `set_socket_path()` now correctly updates the local `DEFAULT_PATH` reference, not just the transport module's copy
- **Fix:** cross-test DB isolation — `_stats_conn` reset on env change prevents stats pollution

## v0.5.3 (2026-06-13)

- **Hermes:** transparent_cache=transparent now default in setup.sh
- **setup.sh:** detects Claude Code, Cursor, OpenCode, Cline — asks per-agent to write config snippets
- **OpenCode:** auto-updates opencode.json instructions
- **README:** explains why agents don't pick cached_read (training exposure, not bias/limitation)
- **README:** config snippets for all 5 supported agents
- **Docs:** docs/HERMES_TRANSPARENT_CACHE.md (DE/EN, risks, config)

## v0.5.2 (2026-06-11)

- **Security:** TOOLRECALL_ALLOW_SENSITIVE env override for _is_sensitive_path()
- **Security:** SECURITY.md — Interface Exposure & Default Transport Security
- **Cleanup:** vitest + happy-dom removed from experimental browser extension (CVE fix applied first)
- **Cleanup:** uv.lock untracked, hardcoded paths in tests/uninstallers replaced
- **Docs:** README flow diagram, elevator pitch, HOW_IT_WORKS.md, APPENDIX.md
- **Docs:** stale terminal-cache claims fixed across README and doc files

## v0.5.1 (2026-06-11)

- **Feature:** browser-extension + api-cache (experimental)
- **Cleanup:** unused imports removed across cache.py, daemon.py, client.py, proxy.py, docs.py
- **Publish:** v0.5.1 on PyPI

## v0.5.0 (2026-06-11)

- **VS Code Extension** — transparent file-read caching via ToolRecall
- **Windows Compatibility** — native TCP fallback, no WSL needed
- **Pluggable hardening** — SHA256 hash mode, shell fallback logging, env overrides
- **Daemon reliability** — fork-safe executor, silent crash fix, watchdog auto-healing, systemd service
- **CLI:** toolrecall init, toolrecall serve --help, TOOLRECALL_* env vars
- **Security:** 4 audit findings fixed (CORS, token leak, null-byte, lazy import)
- **Dead code removed:** cmd_gc(), index_hermes_memory()
- **176/176 tests passing**

## v0.4.9 (2026-06-10)

- Fix: 2 more _send → send (cached_patch, docs_get_page)

## v0.4.8 (2026-06-10)

- Fix: 7x _send → send rename, tokens_saved keyerror
- Fix: nginx is optional, README cleanup

## v0.4.7 (2026-06-10)

- **Zero deps:** pip install toolrecall adds nothing but toolrecall
- README rewrite, log banner fix, minimal allowed_paths

## v0.4.6 (2026-06-10)

- **Agent-agnostic defaults:** no Hermes paths in config.toml
- macOS ready, platform support table in README

## v0.4.5 (2026-06-10)

- GitHub MCP opt-in, tool_access_control default empty
- Request logging

## v0.4.4 — skipped (version bump)

## v0.4.3 (2026-06-09)

- Version bump (0.4.0 → 0.4.3), deprecation cleanup
- README URL fix, "What Is ToolRecall?" section added

## v0.4.2 (2026-06-09)

- Rename: Sandbox WAF → MCP Keyword Filter
- Fix doc exaggeration

## v0.4.1 (2026-06-09)

- Uninstaller, update script
- refresh_file + bypass_cache for cached_read

## v0.4.0 (2026-06-09)

- **Initial public release**
- MCP multiplexer, FTS5 knowledge base, zero-trust WAF
- Hermes init_script integration (separate mode)
- Daemon with SQLite + In-Memory LRU
- HTTP proxy (forward + bridge)
- Security audit: WAF, path canonicalization, sensitive file blocklist
- 155 tests

## v0.3.0 (2026-06-08)

- **MCP Multiplexer:** persistent subprocess manager for external MCP servers (github, time, fetch, sequential-thinking)
- **MCP Server:** security-gated tools with AST injection check, cognitive scan, keyword access control
- **Daemon:** systemd service, config.toml servers_config, .env loader
- **Perf:** lazy MCP server start + idle timeout
- **Benchmarks:** 55K tokens cached, 89% hit rate in production session

## v0.2.0 (2026-06-07)

- **Hybrid LRU + SQLite:** two-tier cache (in-memory for speed, SQLite for persistence across sessions)
- **MCP Cache:** transparent caching for MCP tool calls with TTL per server
- **Windows compatibility:** TCP fallback for platforms without AF_UNIX
- **Hermes integration:** init_script hooks for transparent_mode
- **Benchmarks:** detailed latency/cost analysis

## v0.1.0 (2026-06-06)

- **Initial prototype:** SQLite-backed file read cache with mtime invalidation
- **Unix Domain Socket transport:** fast IPC between daemon and client
- **Basic CLI:** toolrecall status, invalidate
- **Proxy:** simple HTTP caching proxy
