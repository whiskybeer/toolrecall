# ToolRecall — Deterministic Tool Cache for LLM Agents

Your agent reads the same file 10 times in a session. Each read goes to disk, returns to the LLM, and inflates your context window. That's 10× the tokens for the same content.

ToolRecall sits between your agent and the OS (or your API provider). On repeat calls it serves cached results from local SQLite instead of re-executing commands or re-sending requests. Byte-identical outputs mean every API call qualifies for provider prefix-caching discounts (up to 90% at Anthropic/OpenAI).

**1 tick instead of 4:** A file read normally needs `stat → open → read → close`. ToolRecall needs only `stat` (mtime check) — on cache hit the bytes come from memory, bypassing disk entirely.

> **⚠️ Best fit: stateless & open-source agents (Hermes, OpenCode, Cline, Aider, herdr)**
>
> ToolRecall excels where agents have limited context budgets and benefit from deterministic cache + MCP multiplexing. If you run **Claude Code** or **Codex CLI**, the shim and MCP bridge can cause stale-state issues — those agents manage their own in-memory tool tracking natively. See [Agent Compatibility](docs/AGENT_COMPATIBILITY.md).

**Zero pip dependencies. Python 3.11+ stdlib only.** 76 KB install. Everything starts automatically.

```bash
pipx install toolrecall
toolrecall setup          # One-shot: config → systemd → shim → daemon start
# Done — every agent on this machine now benefits
```

> **Zero config mode:** After `toolrecall setup`, every command like `toolrecall status`, `toolrecall mcp`, or `toolrecall serve` auto-starts the daemon if it isn't running. You never need to think about it.

---

## What It Does

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

**Real-agent debug loop (10 turns, 5 writes):** A Hermes agent fixing bugs in ToolRecall's own code shows **36.4% input token savings** — 63,326 input tokens without TR → 40,270 with TR. Write-invalidation resets the cache on every edit, so savings are lower than read-only benchmarks (98%+) but reflect actual edit-heavy sessions. At 50 turns with the same write frequency, estimated savings climb to ~68%. [Full methodology](docs/REAL_AGENT_BENCHMARK.md).

Source: [Benchmark](docs/BENCHMARK.md)

---

## Agent Integration — zero-config for any agent

ToolRecall's daemon provides three agent-agnostic caching layers. None require per-agent configuration.

### Layer 1: Python Shim (transparent, any Python agent)

After `toolrecall setup`, Python processes with the `.pth` shim installed auto-cache `open()` and `subprocess.run()` through ToolRecall. Hermes, Aider, Cline, Google ADK — all benefit once the shim is active (`toolrecall shim --install`).

```bash
pipx install toolrecall
toolrecall setup              # One-shot: shim + daemon
toolrecall shim --install     # Enable .pth shim (opt-in)
# Done — every Python process now transparently caches
```

> Node.js agents (Claude Code, Codex CLI, OpenCode) are unaffected by the shim — see [Agent Compatibility](docs/AGENT_COMPATIBILITY.md) for their recommended integration.

### Layer 2: MCP Bridge (any MCP-compatible agent)

Connect **any MCP agent** by registering one server. The same config works for all agents.

```json
// ~/.claude/settings.json  or  ~/.cursor/mcp.json  or  ~/.config/cline/mcp_settings.json
// or any other MCP agent config
{
  "mcpServers": {
    "toolrecall": {
      "command": "toolrecall",
      "args": ["mcp"]
    }
  }
}
```

For OpenCode (v1.17+), `toolrecall setup` writes this automatically to `~/.opencode/opencode.jsonc`:

```jsonc
// ~/.opencode/opencode.jsonc
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "toolrecall": {
      "type": "local",
      "command": "toolrecall",
      "args": ["mcp"],
      "enabled": true
    }
  }
}
```

**Hermes Agent:** Hermes already ships with ToolRecall built in — the tools `cached_read`, `cached_terminal`, `mcp_call`, etc. are available directly in your toolset.

**Aider:**
```bash
aider --mcp-toolrecall
```

All agents share **one daemon** and **one cache** — no duplication, no conflict.

### Layer 3: Go Client (`tr` binary) — for any language or shell

**For OpenCode, Claude Code, Codex CLI, herdr panes, or any non-Python agent:** The `tr` binary connects directly to the ToolRecall daemon over UDS. Cached file reads, terminal commands, and status checks — all from the shell, no Python runtime needed.

```bash
tr read main.py            # Cached file read
tr cat /etc/os-release     # Alias for read
tr term "hostname"         # Cached terminal command
tr status                  # Daemon health & cache stats
tr ping                    # Fast connectivity check
tr read --bypass file.py   # Force fresh read
tr read --refresh file.py  # Alias for bypass
tr write /tmp/test.txt "hello"  # Write (invalidates cache)
```

Use it when: **herdr panes** (every agent in any pane uses `tr` directly), CI/CD pipelines, Rust/Ruby/Java agents, any shell script.

```bash
# Build from source
cd go-client && go build -o /usr/local/bin/tr .
```

See [Go Client](go-client/README.md) for full details.

> ⚠️ **Claude Code users:** Adding ToolRecall as an MCP server can cause stale-state issues in code edit loops. See [Agent Compatibility](docs/AGENT_COMPATIBILITY.md) before configuring.

---

## Architecture

```
┌─────────────┐  ┌─────────────┐  ┌──────────────┐
│ Python Shim  │  │  MCP Bridge │  │ Forward Proxy │
│ open() → uds │  │ stdio → uds │  │  HTTP :8569   │
└──────┬───────┘  └──────┬──────┘  └───────┬───────┘
       └─────────────────┼─────────────────┘
                         ▼
              ┌─────────────────────┐
              │   ToolRecall Daemon  │
              │  ┌─────┐ ┌───────┐  │
              │  │ LRU │ │ SQLite│  │
              │  └─────┘ └───────┘  │
              │  ┌───────────────┐  │
              │  │ MCP Multiplex │  │
              │  └───────────────┘  │
              │  ┌───────────────┐  │
              │  │ Security Gate  │  │
              │  └───────────────┘  │
              │  ┌───────────────┐  │
              │  │Context Tracker│  │
              │  └───────────────┘  │
              └─────────────────────┘
```

**Shim layer (at the OS level):** When `tr_shim.pth` is in `site-packages`, Python processes auto-patch `builtins.open()` and `subprocess.run()` — no imports needed. Hermes, Aider, Cline transparently benefit. (Claude Code, Codex CLI, and OpenCode are Node.js — the Python shim doesn't apply.)

**Daemon layer (process level):** Holds the hybrid in-memory LRU + SQLite WAL cache, the MCP Multiplexer (manages subprocesses for external MCP servers), the Forward Proxy (caches full API responses via body hash), and the Security Gate (path allowlist, sensitive file blocklist, cognitive scan, AST injection check).

**How they work together:**

1. **Python process** calls `open("file.py")` → Shim intercepts → `cached_read()` via Daemon UDS → returns cached bytes or reads from disk
2. **Agent** calls `cached_read()` via MCP → Daemon → same cache (shared with Shim)
3. **Any SDK** sends API request to `localhost:8569` → Forward Proxy hashes body → checks same SQLite cache

### MCP Multiplexer

When running multiple agents on the same machine (5 Claude Code sessions + 3 Cursor instances), each one normally spawns its own subprocess for every MCP server (GitHub, Postgres, time…). That's 10× the RAM for the same tool.

The daemon's multiplexer shares one subprocess per server across **all** agents:

- **Lazy loading:** servers boot on first call, not at daemon start (~0.01s vs ~1.7s per server)
- **Idle timeout:** inactive subprocesses killed after 15 min (configurable)
- **Failure isolation:** one server crash doesn't affect others (auto-reconnect, max 3 attempts)
- **Secrets:** API tokens loaded from `~/.toolrecall/.env`, never exposed to the LLM
- **Auto-resolution:** Server names auto-resolve from the built-in registry — no `command`/`args` needed for common servers

All agents connect to **one** MCP server in their config: `toolrecall mcp`.

#### Quick Config Example

```toml
# ~/.config/toolrecall/toolrecall.toml
[mcp_multiplex]
servers = ["time", "github", "fetch"]
```

#### Built-in Servers (zero deps)

| Server | What it does |
|--------|-------------|
| `time` | Current time in any timezone — stdlib only |
| `github` | GitHub API (create repo, push files, list commits) — `urllib` only |
| `sequential-thinking` | Reasoning validation, contradiction detection — no network |
| `fetch` | Fetch URLs — stdlib only (`urllib.request`), 500KB configurable limit via `TOOLRECALL_FETCH_MAX_BYTES` |

#### External Servers (needs `uvx`)

| Server | Package |
|--------|---------|
| `filesystem` | `mcp-server-filesystem` — safe file access |
| `git` | `mcp-server-git` — Git operations |
| `memory` | `mcp-server-memory` — knowledge graph |
| `brave-search` | `@anthropic/mcp-server-brave-search` — web search |
| `playwright` | `@playwright/mcp` — browser automation |
| `slack` | `mcp-server-slack` — Slack workspace |

See [MCP Multiplexer](docs/MCP_MULTIPLEXER.md) for full configuration details.

---

## One-Time Setup

ToolRecall should be installed once per machine, then it works transparently for all agents.

```bash
pipx install toolrecall         # installs CLI + Shim (.pth file activates on next Python start)
toolrecall setup                # config → systemd service → shim → daemon start
```

That's it. Now **opt-in Python processes** (with the `.pth` shim installed) transparently cache file reads and terminal commands through ToolRecall. To enable the shim: `toolrecall shim --install`.

### What `toolrecall setup` does

| Step | Details |
|------|---------|
| **Config** | Creates `~/.config/toolrecall/toolrecall.toml` with default-deny security |
| **Systemd** | Generates `~/.config/systemd/user/toolrecall-daemon.service` (enables auto-restart) |
| **Shim** | Installs `tr_shim.pth` in your site-packages — Python processes auto-cache |
| **Daemon** | Starts the cache daemon (background process with LRU + SQLite) |

### What happens on every CLI command

Every `toolrecall` command that needs the daemon (`status`, `mcp`, `serve`, `stats`, etc.) automatically:

1. **Checks if the shim is installed** — auto-installs it if missing
2. **Checks if the daemon is running** — auto-starts it if not

This means you can run `toolrecall status` on a fresh install and it "just works" — no extra steps.

### Daemon auto-start (fallback chain)

| Try | Method | When |
|-----|--------|------|
| 1 | `systemctl --user start toolrecall-daemon` | Linux with systemd |
| 2 | `os.fork()` + `run_daemon()` | Docker, macOS, Codespaces |
| 3 | `subprocess.DETACHED_PROCESS` | Windows |

---

## Forward Proxy (API-level caching)

Cache API responses before they leave your machine. The forward proxy starts **automatically** with the daemon — no extra command needed.

```bash
# Point any OpenAI-compatible SDK at the forward proxy
export OPENAI_BASE_URL=http://localhost:8569/v1
```

| Provider SDK | How to connect | Token savings |
|-------------|---------------|---------------|
| **Any OpenAI-compatible client** | Set base URL to `http://localhost:8569/v1` | **Zero tokens consumed** — cache hit never reaches the provider |
| **Custom port** | `toolrecall serve --port 9090` | Same |

Supported providers: OpenAI, Anthropic, Google Gemini, DeepSeek, xAI, Mistral, Groq, Together, OpenRouter. See [Forward Proxy docs](docs/FORWARD_PROXY.md) for the full provider list and usage examples.

### FTS5 Knowledge Base — Query via MCP or HTTP

The SQLite FTS5 index built by `toolrecall index` is queryable by the agent itself:

- **MCP tool** (active when MCP bridge is connected): `mcp_toolrecall_docs_search(query="...")` — returns BM25-ranked results with snippets
- **HTTP endpoint** (active when Forward Proxy is running): `GET http://localhost:8569/__docs/search?q=<query>` — returns JSON, any HTTP-speaking client can use it

This means the agent can search its own cached docs, memory stores, and indexed files without leaving the tool loop. Index with `toolrecall index`. See [Knowledge DB](docs/KNOWLEDGE_DB.md).

---

## Security

ToolRecall doesn't prevent prompt injection — it cages the consequences:

- **Default-deny path allowlist:** Without config, NO paths are readable. `toolrecall init` prompts for paths interactively.
- **Sensitive file blocklist:** `.env`, `.ssh/`, `.pem`, `.aws/`, etc. are blocked even inside allowed paths.
- **`allow_terminal`** (default: `false`): allows read-only commands matching the regex allowlist (27 patterns for `ls`, `cat`, `git status`, etc.). Set `true` to enable terminal caching.
- **`os.path.realpath()`:** catches `../../../etc/shadow` traversal before OS is touched.
- **Cognitive Pre-Fight:** Deterministic regex scan on MCP tool arguments for override instructions, jailbreak tags, exfiltration URLs. Zero LLM, ~0.001ms hot path.
- **AST injection check:** Parses tool arguments as Python AST — blocks `exec()`, `eval()`, `__import__()` calls.
- **Daemon IPC via UDS:** No open ports (POSIX), immune to SSRF. The forward proxy listens on TCP `:8569` for HTTP API caching — intentional, separate from daemon transport.
- **Fail-closed fallback:** If the daemon is unreachable, the client refuses gated operations (terminal, unrestricted reads) instead of silently allowing them.

See [Security Architecture](SECURITY.md) for the full trust boundary.

---

## Quick Reference — CLI

```
toolrecall setup          One-shot: config + systemd service + shim + daemon start  [required once]
toolrecall init           Create default config.toml and .env
toolrecall status         Cache status and stats               [auto-starts daemon]
toolrecall stats          Detailed cache statistics (JSON)     [auto-starts daemon]
toolrecall invalidate     Clear all caches                     [auto-starts daemon]
toolrecall restart        Health check + clean daemon restart  [auto-starts daemon]
toolrecall mcp            Start MCP Bridge                     [auto-starts daemon]
toolrecall serve          Forward proxy (cache API responses)  [auto-starts daemon]
toolrecall serve --port 9000  Forward proxy on custom port
toolrecall debug          Start debug/demo server              [auto-starts daemon]
toolrecall index          Build/update FTS5 knowledge database [auto-starts daemon]
toolrecall config-set     Set a config value
toolrecall daemon         Start/stop/manage cache daemon
toolrecall shim           Install/uninstall OS-level cache shim (.pth file)
toolrecall nginx          Generate nginx config
```

---

## Configuration

TOML (stdlib `tomllib`) or YAML (optional, requires `pyyaml`).

```toml
# ~/.config/toolrecall/toolrecall.toml (created by toolrecall init)
[norm]
# Cache key normalization (v0.9.0) — deterministic JSON sorting + noise stripping.
# When enabled, tool call arguments are normalized before cache key generation:
# keys sorted, whitespace stripped, timestamps/session IDs removed.
# This broadens cache hits when agents rephrase or reorder arguments.
# ⚠️ Changes existing cache keys — existing entries become orphans.
enabled = false

[mcp]
allowed_paths = ["/home/user/projects"]  # Add your project dirs — default-deny!
allow_terminal = false

# Terminal command allowlist — only commands matching these regex patterns
# are eligible for caching. See config.toml for the full list.
allow_invalidate = false
default_ttl = 60

[mcp_multiplex]
enabled = true
servers = ["time", "sequential-thinking"]

[forward_proxy]
# Forward proxy starts on :8569 automatically with the daemon
```

`TOOLRECALL_*` environment variables override TOML.

---

## Platform Support

| Platform | Transport | Status |
|----------|-----------|--------|
| **Linux** | Unix Domain Sockets | ✅ Tested in CI |
| **macOS** | Unix Domain Sockets | ✅ Should work (POSIX). Not in CI. |
| **Windows** | TCP localhost:8568 fallback | ⚠️ Experimental — not in CI |

---

## Contributing

```bash
git clone https://github.com/whiskybeer/toolrecall.git
cd toolrecall
make setup      # one-time: install dev deps
make test       # run tests
make check      # lint + format check
```

See the [Testing Guide](docs/TESTING.md) and [Makefile](./Makefile) for all targets.

## Uninstall

```bash
toolrecall shim --uninstall          # Remove .pth from site-packages
systemctl --user stop toolrecall-daemon
systemctl --user disable toolrecall-daemon
pipx uninstall toolrecall
rm -rf ~/.toolrecall ~/.config/toolrecall
```

---

## Documentation

- [Agent Compatibility](docs/AGENT_COMPATIBILITY.md) — per-agent value, config, and caveats
- [Architecture](docs/ARCHITECTURE.md) — daemon design, layers, IPC
- [Architecture Diagram](docs/ARCHITECTURE_DIAGRAM.md) — system and sequence diagrams, token costs, Context Tracker
- [CLI Reference](docs/CLI.md) — all subcommands explained
- [Configuration Reference](docs/CONFIG_REFERENCE.md) — config.toml, config.py, all env vars
- [Context Tracker](docs/CONTEXT_TRACKER.md) — checkpoint-based dirty-file tracking, O(n²) breakdown
- [How It Works](docs/HOW_IT_WORKS.md) — quick technical overview
- [MCP Multiplexer](docs/MCP_MULTIPLEXER.md) — single-daemon MCP management, server registry
- [Testing Guide](docs/TESTING.md) — test philosophy, organization, per-file coverage
- [Benchmark](docs/BENCHMARK.md) — measured performance, token savings
- [Real-Agent Debug Loop](docs/REAL_AGENT_BENCHMARK.md) — edit-heavy session benchmark
- [Knowledge DB](docs/KNOWLEDGE_DB.md) — FTS5 indexing guide
- [Normalizer](docs/NORMALIZER.md) — cache key normalization, deterministic JSON sorting
- [Replay Mode](docs/REPLAY_MODE.md) — record/replay tool calls for deterministic CI testing
- [Docker Deployment](docs/DOCKER.md) — containerized stack
- [Forward Proxy](docs/FORWARD_PROXY.md) — cache API responses by body hash, provider list, usage
- [Security Architecture](SECURITY.md) — WAF details, trust boundary
- [Troubleshooting](docs/TROUBLESHOOTING.md) — common fixes
- [Appendix](docs/APPENDIX.md) — comparison tables, OSI model, ROI, vision, audit
- [Hermes Transparent Cache](docs/HERMES_TRANSPARENT_CACHE.md) — auto-patching for Hermes Agent
- **Framework Adapters:**
  - [Google ADK](docs/google-adk.md) — `@cached_tool` decorator + forward proxy + runtime patch
  - [LangChain / LangGraph](docs/langchain.md) — `ToolRecallCache` BaseCache + callback handler
  - [herdr](docs/herdr.md) — `tr` binary + MCP bridge for any agent in any pane