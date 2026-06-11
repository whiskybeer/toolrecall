# Changelog

## v0.5.0 (2026-06-11)

### 🎉 Major Features

- **VS Code Extension** — transparent file-read caching via ToolRecall. Open a file → cached. Status bar shows `TR: 12H / 3M`. Zero config. ([#extension](vscode-extension/))
- **Windows Compatibility** — native TCP fallback, `.exe`/`.cmd` detection, `windowsHide: true` spawn, PowerShell install docs. No WSL needed. ([docs/WINDOWS_COMPATIBILITY.md](docs/WINDOWS_COMPATIBILITY.md))
- **VS Code vs Claude Code Comparison** — detailed breakdown of what each caches, when to use which. ([docs/VS_CLAUDE_CODE.md](docs/VS_CLAUDE_CODE.md))

### 🔒 Security

- **4 audit findings fixed:** CORS open proxy → removed, token leak in `serverInfo` → stripped, null-byte path traversal → rejected in SecurityGate, lazy import for MCP dependency
- **Pluggable hardening** — configurable SHA256 hash mode (replaces MD5), shell fallback logging, env var overrides for security settings
- **VS Code extension hardening** — OWASP Top 10 coverage, input validation, no shell injection, safe JSON parsing, path traversal prevention
- **Sensitive file blocklist** in SecurityGate extended — blocks `.env`, `.ssh/`, `.pem`, `.key`, `.cert`, credentials files, `.gitconfig`, `.netrc`, `.npmrc`

### 🛠️ Daemon Reliability

- **ThreadPoolExecutor after fork fix** — executor moved from `__init__` (pre-fork) to `start()` (post-fork). Eliminates silent crash from corrupted locks in child process.
- **Silent crash fix** — `start()` and `run_daemon()` now wrap the main loop in `BaseException` + `faulthandler.enable()`. Every crash produces a traceback in logs/systemd journal.
- **Fragile YAML parser fix** — `_parse_hermes_mcp_servers` rewritten: accepts indent 2 or 4, proper `[]` bracket stripping, `try/except` per line (never throws), handles malformed lines gracefully.
- **IPC shutdown/restart** — new `shutdown` and `restart` commands via Unix Socket. Clean PID + socket cleanup, thread-safe `os._exit(0)`.
- **Watchdog auto-healing** — `toolrecall-watchdog.py` now auto-restarts the daemon when unresponsive: `systemctl --user restart` first, direct `Popen` fallback. Runs as `no_agent=true` cron every 10min.
- **Systemd service** — `Restart=on-failure` with `RestartSec=2`. Combined with watchdog, recovery in ~2-10s.

### ⚙️ Configuration & CLI

- **`toolrecall init`** — interactive security setup with banner
- **`toolrecall serve --help`** — full help text with available endpoints
- **`[cache]` and `[security]` config sections** — SHA256 mode, shell logging, env overrides
- **`TOOLRECALL_*` env vars** — override any TOML setting at runtime
- **Dead code removed** — `cmd_gc()` (unused GC command), `index_hermes_memory()` (deprecated wrapper)
- **Port fix** — proxy `--port 0` now correctly reports actual OS-assigned port on stdout

### 📚 Documentation

- `docs/WINDOWS_COMPATIBILITY.md` — full Windows compatibility study with transport differences, crash root cause analysis, auto-healing architecture, footguns
- `docs/VS_CLAUDE_CODE.md` — detailed comparison: cache scope, persistence, security, MCP multiplexing, cost savings
- README restructured — 3-line summary, 4-line quickstart, expanded comparison table, platform support matrix
- VS Code extension README — PowerShell install, other IDE integrations, OWASP security, tested state
- Hermes README with Windows/Node.js install fixes

### 🧪 Testing

- **176/176 tests passing** across all commits
- VS Code extension TypeScript compiles cleanly
- VSIX packaged (19 KB)
- Daemon+proxy integration tested: cached_read (miss→hit), stats, invalidate, blocked paths, non-existent files — all verified
- Daemon crash/restart cycle tested
- systemd user service tested (NRestarts=0)
- Proxy port=0 detection tested end-to-end

### 🐛 Bug Fixes

- Proxy printed `0` as port instead of the actual OS-assigned port → `server.server_port` used
- VS Code extension crashed when toolrecall binary not found → graceful fallback
- Daemon crashed silently (no traceback) on any error → BaseException wrap + faulthandler
- Daemon executor locks corrupted after fork → lazy init in `start()`
- YAML parser crashed on 4-space indent, malformed `[]` brackets, missing `env` keys → fully rewritten
- Daemon "already running" loop with systemd (4393 restarts) → systemd now takes over cleanly