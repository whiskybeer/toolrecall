# ToolRecall — Deterministic Tool Cache for LLM Agents

ToolRecall sits between your agent and the OS. On repeat calls it serves cached results from local SQLite instead of re-executing system commands. Caching is deterministic — byte-identical until mtime/TTL expiry — which qualifies every API call for provider prefix-caching discounts (up to 90% at Anthropic/OpenAI).

**1 tick instead of 4:** A file read normally needs `stat → open → read → close`. ToolRecall needs only `stat` (mtime check) — on cache hit the bytes come from memory, bypassing disk entirely.

**Zero pip dependencies. Python 3.11+ stdlib only.** 76 KB install. One daemon.

```
pip install toolrecall    # Installs nothing but ToolRecall itself
toolrecall init            # Interactive security setup (default-deny paths)
toolrecall daemon &         # Start cache daemon
toolrecall mcp              # Connect any MCP agent (Claude Code, Cursor, Cline, Hermes...)
```

**Requirements:** Python 3.11+ (`sqlite3`, `tomllib`, `json`, `http.server`, `urllib` from stdlib).

---

## What It Does

ToolRecall intercepts tool calls at the daemon level and returns cached results when inputs haven't changed:

| Mechanism | What gets cached | Invalidation |
|---|---|---|
| **File cache** | First disk read per file | `mtime` changes → fresh read |
| **Terminal cache** | Static commands (hostname, whoami, pwd, uname, uptime, df, free, crontab) | TTL-based (default 300s) |
| **MCP cache** | External MCP server responses (GitHub, time, fetch...) | TTL-based (default 60s, per-server override) |
| **Script/Code cache** | `cached_run`, `cached_exec` output | `ttl=0` disables caching |

Dynamic commands (`git`, `ls`, `curl`) and state-changing operations always execute live.

### Measured effect

In a 13-hour session (Hermes + Gemini 3.1 Pro, 386 messages, 13 project files):

- **89% hit rate** (91% file cache): 827 tool calls served from SQLite instead of OS
- **73% fewer file-read tokens** at 3× re-read (~204K → ~55K unique)
- **~81% fewer** at 10× re-read (~630K → ~55K unique)
- **~20 min less wait time** — each cache hit avoids ~1.5s subprocess fork
- **Provider prefix-caching** becomes reliable: byte-identical payloads qualify for Anthropic/OpenAI's up-to-90% discount on every call

Source: [Benchmark](docs/BENCHMARK.md)

---

## Architecture

```
  [ Claude Code ]   [ Cursor IDE ]   [ Hermes Agent ]
         \                |                /
          \               |               /
        +───────────────────────────────────+
        │  Standard stdio Protocol (Bridge) │  <- Client Layer
        +─────────────────┬─────────────────+
                          │ Unix Domain Socket (Linux/Mac)
                          │ TCP localhost:8568 (Windows)
        +─────────────────▼─────────────────+
        │         ToolRecall Daemon         │  <- Gateway Layer
        │  ┌─────────────────────────────┐  │
        │  │   In-Memory LRU (Cache)     │  │
        │  └──────────────┬──────────────┘  │
        │  ┌──────────────▼──────────────┐  │
        │  │   SQLite WAL (Persistent)   │  │
        │  └─────────────────────────────┘  │
        │  ┌─────────────────────────────┐  │
        │  │   MCP Server Multiplexer    │  │
        │  └──────────────┬──────────────┘  │
        +─────────────────┼─────────────────+
                          │ Lazy-Loaded stdio Subprocesses
        +─────────────────▼─────────────────+
        │ [ Downstream MCP: GitHub / Time ] │  <- Execution Layer
        +───────────────────────────────────+
```

The daemon holds everything: the hybrid in-memory LRU + SQLite WAL cache, the MCP Multiplexer (manages subprocesses for external MCP servers), and the Security Gate (path allowlist, sensitive file blocklist, cognitive scan). All agents share one daemon via Unix Domain Sockets.

See [Architecture](docs/ARCHITECTURE.md) for the full design.

---

## MCP Multiplexer

Instead of each agent spawning separate subprocesses for every MCP server (GitHub, Postgres, time, fetch...), the daemon manages them:

- **Lazy loading:** servers boot on first call, not at daemon start (~0.01s vs ~1.7s per server)
- **Idle timeout:** inactive subprocesses killed after 15 min (configurable)
- **Failure isolation:** one server crash doesn't affect others (auto-reconnect, max 3 attempts)
- **Secrets:** API tokens loaded from `~/.toolrecall/.env`, never exposed to the LLM

Agents connect to **one** MCP server in their config: `toolrecall mcp`.

See [MCP Multiplexer](docs/MCP_MULTIPLEXER.md) for details.

---

## Security

ToolRecall doesn't prevent prompt injection — it cages the consequences:

- **Default-deny path allowlist:** Without config, NO paths are readable. `toolrecall init` prompts for paths interactively.
- **Sensitive file blocklist:** `.env`, `.ssh/`, `.pem`, `.aws/`, etc. are blocked even inside allowed paths.
- **`allow_terminal=false`** (default): drops all `cached_terminal` calls into a void.
- **`os.path.realpath()`:** catches `../../../etc/shadow` traversal before OS is touched.
- **Cognitive Pre-Flight:** Deterministic regex scan on MCP tool arguments for override instructions, jailbreak tags, exfiltration URLs. Zero LLM, ~0.001ms hot path.
- **AST injection check:** Parses tool arguments as Python AST — blocks `exec()`, `eval()`, `__import__()` calls.
- **Daemon IPC via UDS:** No open ports, immune to SSRF.

See [Security Architecture](SECURITY.md) for the full trust boundary.

---

## Quick Reference — CLI (defaults marked, optional marked)

```
toolrecall init            Create default config.toml and .env  [required once]
toolrecall daemon          Start cache daemon                   [required]
toolrecall mcp             Start MCP Bridge                     [required — connects your agent]
toolrecall status          Cache status and stats               [optional]
toolrecall invalidate      Clear all caches                     [optional]
toolrecall reset-stats     Reset statistics counters            [optional]
toolrecall serve           Start HTTP proxy                     [optional — only for agents that can't speak MCP]
toolrecall nginx           Generate nginx config                [optional — only if you want HTTPS in front of the proxy]
toolrecall index           Build/update FTS5 knowledge database [optional]
toolrecall index-dir       Index a directory (e.g. Obsidian)    [optional]
toolrecall config-set      Set a config value                   [optional]
```

---

## Agent Integration

ToolRecall registers its MCP tools under names like `cached_read`, `cached_terminal`, `cached_write`, `cached_patch`. How you connect depends on your agent:

| Agent | How to connect | Cache mode |
|---|---|---|
| **Hermes** (`hermes_init.py`) | Transparent cache patches `read_file` → `cached_read` automatically | ✅ Zero config |
| **Claude Code** | `claude mcp add toolrecall -- toolrecall mcp` then config snippet | ⚡ Config snippet |
| **Cursor** | Add to `.cursorrules` | ⚡ Config snippet |
| **Cline** | Add to `.clinerules` or `cline_mcp_settings.json` | ⚡ Config snippet |
| **Any MCP agent** | Add server `toolrecall: toolrecall mcp` to your MCP config | ⚡ Universal |

### Config snippets

**Claude Code** — add to `~/.claude/claude_dotfiles/claude.md`:
```markdown
ToolRecall is installed. When reading files, use `cached_read` via MCP instead of `read_file`.
When running terminal commands, use `cached_terminal` instead of `terminal`.
```

**Cursor** — add to `.cursorrules` at project root:
```
Use cached_read for file reads (MCP tool, faster on repeats).
Use cached_terminal for terminal commands (MCP tool, TTL-cached).
```

**Cline** — add to `.clinerules` or mention in the initial prompt:
```
When reading files, always use cached_read instead of read_file.
When running terminal commands, use cached_terminal.
```

**Hermes Agent** — transparent mode (monkey-patches native tools automatically):

```toml
# ~/.toolrecall/config.toml
[hermes]
transparent_cache = "transparent"   # default: "separate"
```

Then restart Hermes or type `/reset`. See [Hermes Transparent Cache](docs/HERMES_TRANSPARENT_CACHE.md) for details and risks.

---

## Configuration

TOML (stdlib `tomllib`) or YAML (optional, requires `pyyaml`).

```toml
# ~/.toolrecall/config.toml (minimal config — toolrecall init creates a full one)
[mcp]
allowed_paths = ["/home/user/projects"]  # Add your project dirs — default-deny!
allow_terminal = false
default_ttl = 60

[mcp_multiplex]
enabled = true
servers = ["time", "fetch"]  # Enable MCP servers; GitHub needs GITHUB_TOKEN in .env

[proxy]
# Proxy binds to 127.0.0.1 (localhost only).
# bind = "127.0.0.1"                      # localhost only
# port = 8567

[nginx]
# nginx is OPTIONAL — only needed if you want HTTPS/SSL in front of the proxy.
# Example: you host and code on the same machine; the proxy listens on localhost
# and nginx terminates SSL + forwards to it. Most users never need this.
# site_name = "toolrecall"
# domain = "example.com"
# ssl = false
```

`TOOLRECALL_*` environment variables override TOML.

---

## Uninstall

```bash
pip uninstall toolrecall
python3 scripts/uninstall.py --force
```

Removes: daemon, systemd service, config, cache DB, logs.

---

## Platform Support

| Platform | Transport | Status |
|----------|-----------|--------|
| **Linux** | Unix Domain Sockets | ✅ Tested in CI |
| **macOS** | Unix Domain Sockets | ✅ Should work (POSIX). Not in CI. |
| **Windows** | TCP localhost:8568 fallback | ⚠️ Core + transport tested. CLI works. |

---

## Documentation

- [Architecture](docs/ARCHITECTURE.md) — daemon design, layers, IPC
- [How It Works](docs/HOW_IT_WORKS.md) — quick technical overview
- [MCP Multiplexer](docs/MCP_MULTIPLEXER.md) — single-daemon MCP management
- [Benchmark](docs/BENCHMARK.md) — measured performance, token savings
- [Knowledge DB](docs/KNOWLEDGE_DB.md) — FTS5 indexing guide
- [Docker Deployment](docs/DOCKER.md) — containerized stack
- [Security Architecture](SECURITY.md) — WAF details, trust boundary
- [Troubleshooting](docs/TROUBLESHOOTING.md) — common fixes
- [Appendix](docs/APPENDIX.md) — comparison tables, OSI model, ROI, vision, audit
- [Hermes Transparent Cache](docs/HERMES_TRANSPARENT_CACHE.md) — auto-patching for Hermes Agent

[^notall]: Not all agents tested yet — please [report bugs](https://github.com/whiskybeer/toolrecall/issues).