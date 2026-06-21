# ToolRecall — Deterministic Tool Cache for LLM Agents

ToolRecall sits between your agent and the OS (or your API provider). On repeat calls it serves cached results from local SQLite instead of re-executing system commands or re-sending requests to the LLM. Caching is deterministic — byte-identical until mtime/TTL expiry — which qualifies every API call for provider prefix-caching discounts (up to 90% at Anthropic/OpenAI).

**1 tick instead of 4:** A file read normally needs `stat → open → read → close`. ToolRecall needs only `stat` (mtime check) — on cache hit the bytes come from memory, bypassing disk entirely.

**Zero pip dependencies. Python 3.11+ stdlib only.** 76 KB install. One daemon.

```
pip install toolrecall    # Installs nothing but ToolRecall itself
toolrecall init            # Interactive security setup (default-deny paths)
toolrecall daemon &         # Start cache daemon
```

**Two ways to use (both on by default — no extra command needed):**

| Path | What it does | How to connect | Default |
|------|-------------|---------------|---------|
| **Forward proxy** | Intercepts HTTP requests to API providers (OpenAI, Anthropic, etc.) — caches full responses by body hash. **Zero tokens consumed on cache hit.** | `export OPENAI_BASE_URL=http://localhost:8569` — or set any SDK's base URL | ✅ On (`:8569`) |
| **MCP bridge** | Caches tool output (file reads, terminal commands) — agent connects as an MCP client | Add to `~/.claude/.mcp.json` or run `toolrecall mcp` | ✅ On (stdio) |

**Requirements:** Python 3.11+ (`sqlite3`, `tomllib`, `json`, `http.server`, `urllib` from stdlib).

---

## What It Does

ToolRecall intercepts tool calls at the daemon level and returns cached results when inputs haven't changed:

| Mechanism | What gets cached | Invalidation | Token saving |
|-----------|----------------|-------------|-----------|
| **File cache** | First disk read per file | `mtime` changes → fresh read | Smaller context → provider prefix-cache discounts |
| **Terminal cache** | Static commands (hostname, whoami, pwd, uname, uptime, df, free, crontab) | TTL-based (default 300s) | Same output never re-sent to LLM |
| **MCP cache** | External MCP server responses (GitHub, time, fetch…) | TTL-based (default 60s, per-server override) | Repeated tool results served from local cache |
| **Script/Code cache** | `cached_run`, `cached_exec` output | `ttl=0` disables caching | Same as file cache |
| **Forward proxy** | Full API responses (chat completions to OpenAI, Anthropic, DeepSeek…) | Body hash — same request → same response | **Zero tokens consumed** — cache hit never reaches the provider |

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
  [ Claude Code ]   [ Cursor IDE ]   [ Hermes Agent ]   [ Any LLM Client ]
         \\                |                |               /
          \\               |               |              /
           \\              |               |             /
        +──────────────────────────────────────────────────────────+
        │  Standard stdio MCP   OR   HTTP (OPENAI_BASE_URL proxy) │
        +──────────────────────────────────────────────────────────+
                          │ Unix Domain Socket (Linux/Mac)
                          │ TCP localhost:8568 (Windows)
        +────────────────▼──────────────────────────────────+
        │         ToolRecall Daemon                         │
        │  ┌─────────────────────────────┐                   │
        │  │   In-Memory LRU (Cache)     │                   │
        │  └──────────────┬──────────────┘                   │
        │  ┌──────────────▼──────────────┐                   │
        │  │   SQLite WAL (Persistent)   │                   │
        │  └─────────────────────────────┘                   │
        │  ┌─────────────────────────────┐                   │
        │  │   MCP Server Multiplexer    │                   │
        │  └──────────────┬──────────────┘                   │
        +─────────────────┼──────────────────+
                          │ Lazy-Loaded stdio Subprocesses
        +─────────────────▼──────────────────+
        │ [ Downstream MCP: GitHub / Time ]  │
        +────────────────────────────────────+
```

The daemon holds everything: the hybrid in-memory LRU + SQLite WAL cache, the MCP Multiplexer (manages subprocesses for external MCP servers), the Forward Proxy (caches full API responses via body hash), and the Security Gate (path allowlist, sensitive file blocklist, cognitive scan).

All agents share one daemon via either:
- **MCP Bridge** (`toolrecall mcp`) — the agent connects as an MCP client and uses `cached_read`, `cached_terminal` etc.
- **Forward proxy** (auto-started on `:8569`) — the agent's API calls go to `localhost:8569` instead of `api.anthropic.com`. The proxy hashes the request body, checks the cache, and on a hit returns the cached response without ever contacting the provider.

See [Architecture](docs/ARCHITECTURE.md) for the full design.

---

## MCP Multiplexer

When running multiple agents on the same machine (5 Claude Code sessions + 3 Cursor instances), each one normally spawns its own subprocess for every MCP server (GitHub, Postgres, time…). That's 10× the RAM for the same tool.

The daemon's multiplexer shares one subprocess per server across **all** agents:

- **Lazy loading:** servers boot on first call, not at daemon start (~0.01s vs ~1.7s per server)
- **Idle timeout:** inactive subprocesses killed after 15 min (configurable)
- **Failure isolation:** one server crash doesn't affect others (auto-reconnect, max 3 attempts)
- **Secrets:** API tokens loaded from `~/.toolrecall/.env`, never exposed to the LLM

All agents connect to **one** MCP server in their config: `toolrecall mcp`.

See [MCP Multiplexer](docs/MCP_MULTIPLEXER.md) for configuration details.

**When to use:** You run 3+ agents simultaneously on the same machine and they share the same MCP tools.
**When to skip:** Single agent setup — each agent manages its own MCP servers fine.

---

## Security

ToolRecall doesn't prevent prompt injection — it cages the consequences:

- **Default-deny path allowlist:** Without config, NO paths are readable. `toolrecall init` prompts for paths interactively.
- **Sensitive file blocklist:** `.env`, `.ssh/`, `.pem`, `.aws/`, etc. are blocked even inside allowed paths.
- **`allow_terminal=false`** (default): drops all `cached_terminal` calls into a void.
- **`os.path.realpath()`:** catches `../../../etc/shadow` traversal before OS is touched.
- **Cognitive Pre-Fight:** Deterministic regex scan on MCP tool arguments for override instructions, jailbreak tags, exfiltration URLs. Zero LLM, ~0.001ms hot path.
- **AST injection check:** Parses tool arguments as Python AST — blocks `exec()`, `eval()`, `__import__()` calls.
- **Daemon IPC via UDS:** No open ports, immune to SSRF.

See [Security Architecture](SECURITY.md) for the full trust boundary.

---

## Quick Reference — CLI

```
toolrecall init            Create default config.toml and .env  [required once]
toolrecall daemon          Start cache daemon (also starts MCP + forward proxy) [required]
toolrecall mcp             Start MCP Bridge                     [connect any MCP agent]
toolrecall serve           Forward proxy (cache API responses)  [auto-started with daemon; use for custom port]
toolrecall debug           Start debug/demo server (test cached_read/term via curl)
toolrecall status          Cache status and stats               [optional]
toolrecall invalidate      Clear all caches                     [optional]
toolrecall reset-stats     Reset statistics counters            [optional]
toolrecall nginx           Generate nginx config                [optional]
toolrecall index           Build/update FTS5 knowledge database [optional]
toolrecall index-dir       Index a directory (e.g. Obsidian)    [optional]
toolrecall config-set      Set a config value                   [optional]
```

---

## Agent Integration

### Forward proxy (API-level caching)

Cache API responses before they leave your machine. The forward proxy starts **automatically** with the daemon — no extra command needed.

```bash
toolrecall daemon &                  # also starts forward proxy on :8569
export OPENAI_BASE_URL=http://localhost:8569
```

| Agent | How to connect | Token savings |
|-------|---------------|---------------|
| **Any LLM client** | `export OPENAI_BASE_URL=http://localhost:8569` | **Zero tokens consumed** — cache hit never reaches the provider |
| **Custom port** | `toolrecall serve --port 9090` if you need a different port | same |

### MCP Bridge (tool-level caching)

ToolRecall registers MCP tools like `cached_read`, `cached_terminal`, `cached_write`, `cached_patch`. The agent *chooses* to use them.

| Agent | How to connect | Token savings |
|-------|---------------|---------------|
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

[nginx]
# nginx is OPTIONAL — only needed if you want HTTPS/SSL in front of the proxy.
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