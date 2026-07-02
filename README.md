# ToolRecall — Deterministic Tool Cache for LLM Agents

ToolRecall sits between your agent and the OS (or your API provider). On repeat calls it serves cached results from local SQLite instead of re-executing system commands or re-sending requests to the LLM. Caching is deterministic — byte-identical until mtime/TTL expiry — which qualifies every API call for provider prefix-caching discounts (up to 90% at Anthropic/OpenAI).

**1 tick instead of 4:** A file read normally needs `stat → open → read → close`. ToolRecall needs only `stat` (mtime check) — on cache hit the bytes come from memory, bypassing disk entirely.

**Zero pip dependencies. Python 3.11+ stdlib only.** 76 KB install. One daemon.

```bash
# Install — no system deps beyond Python 3.11+ stdlib:
pip install toolrecall
toolrecall init            # Interactive security setup (default-deny paths)
toolrecall daemon &         # Start cache daemon (use --foreground for systemd)
```

> **Debian/Ubuntu (Python ≥3.11):** If `pip install` hits `externally-managed-environment`, the cleanest fix is:
> `pip install --break-system-packages toolrecall`  *(ToolRecall has zero deps — safe to override)*

**Two ways to use (both on by default — no extra command needed):**

| Path | What it does | How to connect | Default |
|------|-------------|---------------|---------|
| **Forward proxy** | Intercepts HTTP requests to API providers (OpenAI, Anthropic, etc.) — caches full responses by body hash. **Zero tokens consumed on cache hit.** | `export OPENAI_BASE_URL=http://localhost:8569` — or set any SDK's base URL | ✅ On (`:8569`) |
| **MCP bridge** | Caches tool output (file reads, terminal commands) — agent connects as an MCP client. Server names auto-resolve from registry. | Add to `~/.claude/.mcp.json` or run `toolrecall mcp` | ✅ On (stdio) |

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
| **Context Tracker** | Tracks dirty/clean files via checkpoints | In-memory (resets on daemon restart) | **93.8% O(n²) reduction** — drop clean files from context |

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
- **Auto-resolution:** Server names auto-resolve from the built-in registry — no `command`/`args` needed for common servers

All agents connect to **one** MCP server in their config: `toolrecall mcp`.

### Quick Config Example

```toml
# ~/.config/toolrecall/toolrecall.toml
[mcp_multiplex]
servers = ["time", "github", "fetch"]
#  ↑ auto-resolved: time=builtin, github=builtin, fetch=uvx
```

No `[mcp_multiplex.servers_config]` section needed for known servers. Custom servers still use the explicit config.

### Built-in Servers (zero deps)

| Server | What it does |
|--------|-------------|
| `time` | Current time in any timezone — stdlib only |
| `github` | GitHub API (create repo, push files, list commits) — `urllib` only |
| `sequential-thinking` | Reasoning validation, contradiction detection — no network |
| `fetch` | Fetch URLs — stdlib only (`urllib.request`), 500KB configurable limit via `TOOLRECALL_FETCH_MAX_BYTES` |

### External Servers (needs `uvx`)

| Server | Package |
|--------|---------|
| `filesystem` | `mcp-server-filesystem` — safe file access |
| `git` | `mcp-server-git` — Git operations |
| `memory` | `mcp-server-memory` — knowledge graph |
| `brave-search` | `@anthropic/mcp-server-brave-search` — web search |
| `playwright` | `@playwright/mcp` — browser automation |
| `slack` | `mcp-server-slack` — Slack workspace |

See [MCP Multiplexer](docs/MCP_MULTIPLEXER.md) for full configuration details.

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
toolrecall mcp             Start MCP Bridge (or: mcp list to see registry) [connect any MCP agent]
toolrecall serve           Forward proxy (cache API responses)  [auto-started with daemon; use for custom port]
toolrecall debug           Start debug/demo server (test cached_read/term via curl)
toolrecall status          Cache status and stats               [optional]
toolrecall stats           Detailed cache statistics (JSON)     [optional]
toolrecall invalidate      Clear all caches                     [optional]
toolrecall reset-stats     Reset statistics counters            [optional]
toolrecall nginx           Generate nginx config                [optional]
toolrecall index           Build/update FTS5 knowledge database [optional]
toolrecall index-memory    Index agent memory stores (MEMORY.md, USER.md) [optional]
toolrecall index-dir       Index a directory (e.g. Obsidian)    [optional]
toolrecall config-set      Set a config value                   [optional]
toolrecall shim            Install/uninstall OS-level cache shim (.pth file) [optional]
```

---

## Agent Integration

### Forward proxy (API-level caching)

Cache API responses before they leave your machine. The forward proxy starts **automatically** with the daemon — no extra command needed. Works with **any** OpenAI-compatible provider (OpenAI, Anthropic, DeepSeek, OpenRouter, etc.).

```bash
toolrecall daemon &                  # also starts forward proxy on :8569
export OPENAI_BASE_URL=http://localhost:8569/v1   # Any OpenAI-compatible SDK
# or override the base URL in your provider config / client init
```

| Provider SDK | How to connect | Token savings |
|-------------|---------------|---------------|
| **Any OpenAI-compatible client** | `export OPENAI_BASE_URL=http://localhost:8569/v1` | **Zero tokens consumed** — cache hit never reaches the provider |
| **Custom port** | `toolrecall serve --port 9090` if you need a different port | same |

### MCP Bridge (tool-level caching)

ToolRecall registers MCP tools like `cached_read`, `cached_terminal`, `cached_write`, `cached_patch`. Connect **any MCP agent** by adding one server:

```json
{
  "mcpServers": {
    "toolrecall": {
      "command": "toolrecall",
      "args": ["mcp"]
    }
  }
}
```

This single snippet works for **Claude Desktop, Claude Code, Cursor, Cline, Windsurf, Continue, and any MCP-compatible agent** with zero per-agent variations.

| Agent | How to connect | Token savings |
|-------|---------------|---------------|
| **Any MCP agent** | Add the `toolrecall` server to your MCP config (see above) | ✅ Universal |
| **Hermes** (option A) | Set `[hermes] transparent_cache = "transparent"` in `~/.config/toolrecall/toolrecall.toml` | ✅ Zero config |
| **Hermes** (option B) | Add to `~/.hermes/config.yaml`: `mcp_servers: { toolrecall: { command: toolrecall, args: [mcp] } }` | ✅ Agent-agnostic MCP |
| **Shim (agent-agnostic)** | `toolrecall shim --install` patches `open()`/`subprocess.run()` at the OS level | ✅ Works with any agent binary |

---

## Configuration

TOML (stdlib `tomllib`) or YAML (optional, requires `pyyaml`).

```toml
# ~/.config/toolrecall/toolrecall.toml (created by toolrecall init)
[mcp]
allowed_paths = ["/home/user/projects"]  # Add your project dirs — default-deny!
allow_terminal = false
allow_invalidate = false
default_ttl = 60

[mcp_multiplex]
enabled = true
# Server names auto-resolve: time/github/seqthink/fetch = builtin (no deps),
# filesystem/git/memory = external (needs uvx), or override via [mcp_multiplex.servers_config]
servers = ["time", "sequential-thinking"]

[forward_proxy]
# Forward proxy starts on :8569 automatically with the daemon
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
- [Architecture Diagram](docs/ARCHITECTURE_DIAGRAM.md) — system and sequence diagrams, token costs, Context Tracker
- [CLI Reference](docs/CLI.md) — all subcommands explained
- [Configuration Reference](docs/CONFIG_REFERENCE.md) — config.toml, config.py, all env vars
- [Context Tracker](docs/CONTEXT_TRACKER.md) — checkpoint-based dirty-file tracking, O(n²) breakdown
- [How It Works](docs/HOW_IT_WORKS.md) — quick technical overview
- [MCP Multiplexer](docs/MCP_MULTIPLEXER.md) — single-daemon MCP management, server registry
- [Testing Guide](docs/TESTING.md) — test philosophy, organization, per-file coverage
- [Benchmark](docs/BENCHMARK.md) — measured performance, token savings
- [Knowledge DB](docs/KNOWLEDGE_DB.md) — FTS5 indexing guide
- [Docker Deployment](docs/DOCKER.md) — containerized stack
- [Security Architecture](SECURITY.md) — WAF details, trust boundary
- [Troubleshooting](docs/TROUBLESHOOTING.md) — common fixes
- [Appendix](docs/APPENDIX.md) — comparison tables, OSI model, ROI, vision, audit
- [Hermes Transparent Cache](docs/HERMES_TRANSPARENT_CACHE.md) — auto-patching for Hermes Agent

[^notall]: Not all agents tested yet — please [report bugs](https://github.com/whiskybeer/toolrecall/issues).