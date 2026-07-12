# Changelog

## v0.8.8 (2026-07-12)\n\n- **Docs:** README restructured — problem-first hook, quick-start promoted before benchmarks, agent integration dedicated section, herdr added to `tr` binary section and agent compatibility callout\n- **Docs:** Moved cache invalidation reference (3 tables + script/code cache Python API) from README to ARCHITECTURE.md Appendix\n- **Docs:** Replaced mermaid diagram with lighter ASCII art (renders everywhere)\n- **Docs:** FTS5 section moved inside Forward Proxy; MCP Multiplexer moved inside Architecture\n- **Docs:** Security section tightened — added fail-closed fallback bullet, removed orphan `|---` syntax\n\n## v0.8.7 (2026-07-12)

- **Feature:** Cache key normalizer — deterministic JSON sorting, whitespace stripping, noise key removal (timestamps, session IDs). Opt-in via `[norm].enabled = true` or `TOOLRECALL_NORM_ENABLED=true`.
- **Feature:** Replay mode — record and replay agent tool calls for deterministic, offline, zero-cost CI testing. `toolrecall replay record <name>` / `toolrecall replay replay <name>`. Scenarios export as portable JSON.
- **Feature:** Framework adapters — Google ADK (`@cached_tool` decorator), LangChain (`ToolRecallCache` BaseCache + callback handler), herdr (integration guide via `tr` binary + MCP bridge). Thin wrappers around `toolrecall.client`, no new dependencies.
- **Feature:** Go client (`tr` binary) — cached file reads, terminal commands, and status from any language. Connects to the daemon over UDS.
- **Feature:** Forward proxy — cache LLM API responses by request body hash. Set SDK base URL to `http://localhost:8569`.
- **Feature:** Native-named MCP tools — `read_file`, `write_file`, `patch`, `terminal` as aliases for `cached_read`, `cached_write`, `cached_patch`, `cached_terminal`. Agents pick these naturally.
- **Fix:** Shim exclude prefixes now configurable via `[shim].exclude_prefixes` in `toolrecall.toml` or `TOOLRECALL_SHIM_EXCLUDE_PREFIXES` env var. Defaults skip `/tmp/hermes-cwd-*` and `/tmp/hermes-snap-*` (Hermes terminal infra files). Empty list = bypass nothing.
- **Fix:** `context_tokens_saved` column added to `cache_stats` — tracks only cache hits from agent-tool reads (`source="agent_tool"`), not from internal infrastructure reads. Separates actual LLM-context savings from general disk-read avoidance (`tokens_saved`).
- **Fix:** `client.cached_read()` sends `source="agent_tool"` to daemon so agent file reads count toward context token savings.
- **Fix:** `cached_write` and `cached_patch` now invalidate `_file_cache` after writing — prevents stale reads when the shim is active and mtime resolution doesn't change on fast writes.
- **Fix:** `_db.py` singleton now detects `TOOLRECALL_CACHE_DB` env var changes and reconnects — eliminates "no such table" warnings when tests switch DB paths.
- **Fix:** LangChain adapter — fixed serialization recursion bug + e2e tests.
- **Fix:** ADK adapter — fixed `json.loads` deserialization + full e2e tests.
- **Fix:** `test_mcp_bridge.py` — updated tool count assertions (10→14), tool name expectations, replaced `importlib.reload` with `unittest.mock.patch`.
- **Fix:** `test_regression_v078_v0711.py` — updated `TestMCPCacheFS` to use native tool names.
- **Docs:** `HERMES_TRANSPARENT_CACHE.md` — added risk section on infrastructure file noise with config examples, plus new section on visibility-into-agent-behavior side effect.
- **Docs:** `CONFIG_REFERENCE.md` — added `TOOLRECALL_SHIM_EXCLUDE_PREFIXES` to env var table.
- **Docs:** Added model-determinism caveat to replay mode limitations.
- **Docs:** Sharpened Go client messaging — non-Python agents first, herdr support, improved Why table.
- **Docs:** Added data structure section, herdr integration, and what's-not-recorded to replay mode.
- **Docs:** README — Go client, forward proxy provider list, MCP native tools.

## v0.8.6 (2026-07-09)

- **Feature:** `__main__.py` — `python -m toolrecall` now works (used by `_handle_restart` fallback)
- **Feature:** `cached_run` / `cached_exec` docs added — documented alongside `cached_read`/`cached_terminal`
- **Fix:** Shim-cache double-counting — `_check_cache` no longer increments hit/miss stats twice on the same lookup
- **Fix:** Test isolation — UDS path collision, MCP Cache FS daemon startup restored, graceful skip for missing `toolrecall` binary
- **Fix:** `test_context_tracker` skips gracefully when `toolrecall` binary not on PATH
- **Fix:** CORS status in `docs/APPENDIX.md` updated to reflect fixed code
- **Clean:** Removed `go-client/` empty stub, `github-auth-permanent-fix.md`, `test_mcp_transparent_cache.py`
- **Clean:** Removed dead `mcp-legacy` reference from `mcp_bridge.py` docstring
- **Clean:** Removed dead VS Code extension step from uninstaller
- **Docs:** All stale references cleaned across 10+ doc files (hooks.py/store.py, tomli-w, VS Code, google-adk dead links, ARCHITECTURE_DIAGRAM formatting, APPENDIX, AGENT_COMPATIBILITY table, MCP_MULTIPLEXER typo, KNOWLEDGE_DB ASCII→Mermaid)
- **Docs:** pipx as primary install method throughout (README, SECURITY, CLI.md, docstrings, scripts)
- **Docs:** Removed user-specific reference from ARCHITECTURE.md
- **Scripts:** `setup.sh` formatting and output wording improved
- **Scripts:** `uninstall.py` docstring updated for install method clarity

- **Removed:** `hermes_init.py` and `init_scripts` mechanism — Hermes Agent has no `init_scripts` config key, the script was never loaded. The OS-level `.pth` shim (`toolrecall/shim.py`) is the agent-agnostic mechanism for all Python-based agents.
- **Removed:** `patch_shim.py` / `toolrecall_patch.py` — dead `PYTHONSTARTUP` mechanism, `toolrecall_patch` module never existed in the installed package.
- **Removed:** Init script references from `toolrecall setup`, all docs, and uninstaller.
- **Fixed:** Live `~/.toolrecall/config.toml` now has `allow_terminal = true` with 27 read-only regex patterns — terminal cache was blocked because the config had `false` and no allowlist. (Source config was already updated in v0.8.4, but live config was never synced.)
- **Fixed:** Terminal regex patterns now correctly match bare commands (`cat`, `grep`, `find`, etc.) — `^cat\s` didn't match `cat` without args, changed to `^cat(\s+|$)`.
- **Fixed:** `setup.sh` now detects `pipx` (preferred for CLI tools), falls back to `pip`. Hermes section installs the `.pth` shim instead of writing `hermes_init.py`.
- **Fixed:** `scripts/uninstall.py` now checks `pipx list` before `pip show` — handles both install methods.

## v0.8.4 (2026-07-07)

- **Feature:** `toolrecall setup` auto-detects Hermes Agent, Claude Code, OpenCode/Crush — writes MCP config and instruction snippets automatically (Hermes uses the OS-level `.pth` shim, no per-agent config needed)
- **Feature:** Daemon duplicate-instance guard — `run_daemon()` pings the socket before starting, refuses if daemon already responds
- **Feature:** SQLite WAL retry — `_db()` retries once on `SQLITE_BUSY` with 100ms sleep
- **Feature:** 13 E2E tests with real daemon subprocess — lifecycle, cache ops, CLI, stress (10 concurrent, 5x rapid restart), isolated temp socket + DB
- **Docs:** `docs/AGENT_COMPATIBILITY.md` — per-agent value matrix with guidance
- **Docs:** Agent config files for Cline, OpenCode, Aider, Windsurf, Continue
- **Docs:** `tests/README.md` translated to English, full 30-file overview table
- **Docs:** `configs/README.md` rewritten with per-agent sections and 7-agent compatibility table
- **Docs:** All Claude Code references caveated across docs; shim claims scoped to Python agents only
- **Config:** `[tool.pytest.ini_options]` — e2e marker added to pyproject.toml
- **Chore:** `.gitignore` — .hermes, .ruff_cache, .pytest_cache, editor swp files

## v0.8.2 (2026-07-04)

- **Feature:** `toolrecall/toml_serializer.py` — zero-dependency TOML writer (replaces `tomli-w`)
- **Feature:** 42 unit tests for TOML serializer (round-trip verified via stdlib `tomllib`)
- **Refactor:** `config.py` `save_config()` now uses built-in serializer — no `tomli-w` dependency needed
- **Refactor:** `cli.py` `cmd_init()` — default allowed paths now include `/tmp` (in addition to `~/.toolrecall`)
- **Refactor:** `cli.py` `cmd_init()` — generated config now has `allow_terminal = true` by default
- **Fix:** `cli.py` `cmd_restart()` — handles systemd exit -15 (SIGTERM) gracefully with fallback to direct daemon start
- **Docs:** All Mermaid diagrams use plain theme (no `%%{init}` blocks) for GitHub dark mode compatibility
- **Docs:** `configs/README.md` translated from German to English
- **Chore:** `pyproject.toml` v0.8.2

## v0.8.1 (2026-07-01)

- **Feature:** `mcp_fetch.py` — built-in stdlib-only HTTP Fetch MCP server (zero deps, replaces `uvx mcp-server-fetch`)
- **Feature:** `TOOLRECALL_FETCH_MAX_BYTES` env var — configurable content size limit (default 500KB, 0 = no limit)
- **Feature:** MCP Server Registry (`toolrecall/mcp_registry.py`) — auto-resolve server names to commands, no `servers_config` needed
- **Feature:** `toolrecall mcp list` — CLI subcommand to list all registered servers with their source and command
- **Refactor:** `fetch` moved from external (uvx) to built-in server in registry
- **Refactor:** `config.py` — removed `_parse_agent_mcp_servers()` (-110 LOC), auto-resolution replaces Hermes config.yaml fallback
- **Config:** `config.toml` — `servers` default changed to `["time", "github", "sequential-thinking"]`
- **Docs:** MCP_MULTIPLEXER.md — Registry tables updated, fetch is now built-in, env var documented
- **Docs:** SECURITY.md — Fetch Layer OOM mitigation documented
- **Docs:** README.md — built-in/external tables updated, config comment fixed
- **Tests:** +50 new tests (registry, config resolve, fetch env var, PID guard) — 328 total, all passing

## v0.7.2 (2026-06-30)

- **Docs:** Full architecture diagram — system (flowchart) + sequence (read/write/cache) diagrams
- **Docs:** Transport Layer section — UDS vs TCP, framed JSON protocol, TOOLRECALL_TRANSPORT override
- **Docs:** Context Tracker section in architecture diagram + feature table in README
- **Docs:** Removed duplicate deployment section, clean split: Installation vs Deployment (Production)
- **Docs:** README CLI reference — added missing `stats`, `index-memory`, `shim` commands
- **Refactor:** Removed all dashboard/proxy_router/llama_server references from docs
- **Fix:** `start_services.sh` — stale PID cleanup, port check before `--status`

## v0.7.1 (2026-06-29)

- **Feature:** Context Tracker — checkpoint-based dirty-file tracking to break O(n²) context growth (docs/CONTEXT_TRACKER.md)
- **Daemon:** +4 new IPC commands: context_set_checkpoint, context_get_dirty, context_get_stats, context_reset
- **Client:** +4 Python API functions: context_set_checkpoint(), context_get_dirty(), context_get_stats(), context_reset()
- **Daemon:** cached_write/cached_patch auto-mark files as dirty; cached_read auto-marks as read
- **Benchmark:** 93.3% O(n²) reduction confirmed — 19 tests, 277 total (0 regressions)
- **Chore:** bump v0.7.0 → v0.7.1

## v0.7.0 (2026-06-22)

- **Refactor:** Remove all Hermes-specific code — fully agent-agnostic
- **Feature:** Transparent OS-level cache shim via `.pth` file (`toolrecall shim --install`)
- **Shim:** Auto-patches `builtins.open()` + `subprocess.run()` in every Python process
- **Shim:** Zero imports needed — `.pth` loads on interpreter startup
- **Shim:** Per-process disable via `TOOLRECALL_SHIM_DISABLE=1`
- **Config:** agent_home resolution chain: `AGENT_HOME` → `HERMES_HOME` → `~/.hermes`
- **Config:** `[hermes]` section removed, `[mcp_multiplex]` is the universal config
- **Config:** Skill dirs via `TOOLRECALL_SKILL_DIRS` env or `[paths].skill_dirs` config
- **Fix:** FTS5 auto-repair — `docs_search()` detects malformed index, transparently rebuilds, retries
- **Fix:** Multi-threaded DB guard — race condition on concurrent FTS5 index rebuilds
- **Docs:** ARCHITECTURE_DIAGRAM.md — Mermaid sequence diagram (read/write cache flow)
- **Docs:** Universal MCP config snippet works with any SDK (not just OPENAI_BASE_URL)
- **Docs:** OS-level shim documented across all architecture docs

## v0.6.1 (2026-06-19)

- **Cleanup:** removed `vscode-extension/`, `mcp_server.py`, `dataset.py`, `examples/` — legacy/experimental code never used in production
- **Docs:** restructured 27→12 files. Added APPENDIX.md, MCP_MULTIPLEXER.md. Deleted stale/duplicate docs
- **Docs:** Architecture disclaimer, fixed UDS path, fixed dead link (CTO_QUESTIONS→APPENDIX), fixed ASCII table in ARCHITECTURE.md
- **Docs:** ALL docstrings translated German→English (client.py, daemon.py)
- **Docs:** dead comment in cache.py fixed (`# ── 3. Cache miss ──` → `# 2. SQLite hit`)
- **README:** rewritten — agent-agnostic, "1 Tick Instead of 4" hero, required vs optional marked, integration table (Hermes, Claude Code, Cursor, Cline), nginx labeled optional
- **Fix:** XDG_RUNTIME_DIR vs real UID mismatch — `_default_socket_path()` validates against `os.getuid()`
- **Fix:** cache blocked — `allowed_paths` package default changed from `~/.toolrecall` to `/home/hermes`
- **Fix:** `proxy.py` — `run_server()` now binds to passed `bind` arg, not hardcoded `127.0.0.1`
- **Fix:** `daemon.py` — hardcoded `v0.3.0` replaced with `from toolrecall import __version__`
- **Fix:** config.toml — `[REDACTED]` IPs replaced with `127.0.0.1`
- **Fix:** test helper `_patch_transport()` uses `set_socket_path()` instead of direct attribute patching
- **Chore:** `.gitignore` — added `vscode-extension/`
- **Tests:** 258 tests, all passing (was 275, after removing legacy code paths and their tests)

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
