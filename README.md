# ToolRecall — The Deterministic Tool Cache for LLM Agents

**ToolRecall makes agents local-first, cutting OS execution latency by ~1000× on repeat calls and unlocking the 90% server-side prompt caching discount through deterministic byte-identical outputs. Result: ~81% fewer input tokens and ~20 min less waiting per session.**

ToolRecall is a caching layer and security guard for AI agents. It sits between the agent and your tools — SQLite cache for repeated reads, FTS5 knowledge base, MCP multiplexer, and a zero-trust WAF.
**No LLM decides what to cache. Pure stdlib — `pip install toolrecall` adds zero dependencies.** (Python 3.11+ with `sqlite3`/`tomllib`/`json`)

**76 KB, zero pip dependencies, one daemon.**

---

## Quickstart

```bash
pip install toolrecall    # Zero deps — installs nothing but ToolRecall itself
toolrecall init            # Interactive security setup
toolrecall daemon &         # Start cache daemon
toolrecall mcp              # Connect any MCP agent (Claude Code, Cursor, Cline, Hermes...)
```

**Requirements:** Python 3.11+ (stdlib: `sqlite3`, `tomllib`, `json`, `http.server`, `urllib`).

### VS Code Extension (experimental)

> ⚠️ **Experimental.** Works in testing — not yet battle-tested in production. You may encounter edge cases with large workspaces or concurrent file changes.

Install the [ToolRecall Cache extension](vscode-extension) from the VS Code Marketplace for **transparent file-read caching** — every file you open is cached automatically with zero configuration:

```bash
pip install toolrecall                    # Step 1: install core
code --install-extension toolrecall.cache # Step 2: install extension
```

**What it gives you over the CLI alone:**
- Files cache automatically on open — no `cached_read()` calls needed
- Status bar shows live hit/miss ratio (`TR: 12H / 3M`)
- Per-workspace scoping — only your current project is readable
- Auto-invalidation on save — edited files refresh immediately
- Graceful fallback if toolrecall isn't installed

See the [extension README](vscode-extension/README.md) for details.

---

## What ToolRecall IS / IS NOT

| ToolRecall IS | ToolRecall IS NOT |
|---|---|
| ✅ **Deterministic** — byte-exact tool output cache from SQLite, no LLM in the caching loop | ❌ Not an LLM-driven Cache Planner — no second agent deciding what to cache |
| ✅ **MCP Multiplexer** — single daemon manages all external MCP servers | ❌ Not a chronological call-graph |
| ✅ **Zero-Trust WAF** — path canonicalization, secret air-gapping, MCP keyword filter | ❌ Not a vector database — no embeddings, no GPU |
| ✅ **FTS5 Knowledge Base** — zero-dep full-text search over docs and notes | ❌ Not a distributed cache — single-node SQLite |
| ✅ **Deterministic replay** — freeze OS state for 100% reproducible agent runs | ❌ Not a replacement for real-time data |

---

## Why Not an LLM-Powered Cache?

Some caching frameworks use a second LLM — a "Cache Planner" — to classify tools by cacheability. ToolRecall is **deterministic**, not heuristic:

| Failure mode | LLM-Driven Cache | ToolRecall (Deterministic) |
|---|---|---|
| **Misclassification** | LLM guesses `send_message()` is STATIC → messages silently dropped | `ttl=0` means NEVER cache. Binary. |
| **Extra API cost** | Every new tool needs an LLM call to classify | $0 — SQLite FTS5, no API calls |
| **Cold-start latency** | Must analyze tool metadata before first decision | First call executes live, cached on return |
| **Side-effect blindness** | Relies on tool name/description, not behavior | mtime-based auto-invalidation — file edited? next read is fresh. |
| **Reproducibility** | Non-deterministic — same tool classified differently per run | Byte-identical for same args + same mtime |

**The principle:** *Intelligent caching doesn't need an intelligence. It needs a filesystem, a clock, and the honesty to say "I don't know — execute it live."*

---

## The Core Problem: The Context Snowball

LLM context windows are stateless. Everything accumulates.

**Level 1 — File repetition (O(N), linear):**
A 10,000-token file, read once, stays in context for 100 turns: 10K × 100 = **1,000,000 billed input tokens** for the same content.

**Level 2 — The real O(N²) snowball (quadratic):**
Context grows continuously through new tool outputs — not just one file. After 100 turns it hits ~500K tokens. Attention scales at O(N²):

```
Context size → Attention pairs per turn
   10K     →       50 million
  100K     →      5 billion
  500K     →    250 billion   (after 100 turns without ToolRecall)
```

**ToolRecall breaks both curves:**
1. **File cache** → file read once, then ~0.6ms from SQLite → 0 tokens for repeats
2. **Micro-RAG** → agent drops large outputs from active context, re-fetches byte-exact from cache on demand

Result: **81% fewer input tokens + context stays manageable + attention costs flat.**

---

## Universal Agent Compatibility (Drop-In MCP)

ToolRecall exposes a standard `stdio` MCP interface (`toolrecall mcp`). It works with **any** agent — Claude Code, Cursor, Cline, Hermes:

```bash
claude mcp add toolrecall toolrecall mcp
```

No custom plugins. No SDK changes.

---

## Security Architecture (The WAF)

ToolRecall doesn't cure an LLM of being prompt-injected — it cages the agent to neutralize the consequences:

- **Daemon-based IPC:** Unix Domain Sockets (Linux/Mac) or TCP localhost fallback (Windows). No open ports — immune to SSRF.
- **Cryptographic path resolution:** `os.path.realpath` blocks `../../../etc/shadow` before the OS is touched.
- **Execution blackholes:** `allow_terminal = false` drops RCE attempts into a void.
- **Air-gapped secrets:** API keys in `~/.toolrecall/.env` — the LLM never sees them.
- **Default-deny init flow:** `toolrecall init` prompts for allowed paths interactively. Without config, ALL paths are blocked.
- **MCP keyword access control:** `tool_access_control = true` blocks MCP tools whose name contains `write`, `delete`, `push`, etc. Substring match — not process isolation.
- **Cognitive Pre-Flight:** Deterministic prompt-injection scan on MCP tool arguments. Zero LLM, sub-millisecond hot path.

---

## How ToolRecall Compares

ToolRecall does **3 things in one daemon**: cache, WAF, MCP multiplex. Each piece has more polished alternatives — the value is integration.

| Your need | ToolRecall | Alternative |
|---|---|---|
| Token reduction / fewer re-reads | ✅ SQLite+in-memory cache (~0.6ms) | [RTK](https://github.com/thinkerai/rtk) (Rust, hook-based) |
| Context compression | ✅ Micro-RAG (agent drops + re-fetches) | [headroom MCP](https://github.com/nicholasgriffintn/headroom) |
| Code/doc search | ✅ FTS5 (BM25, zero deps) | [serena](https://github.com/SerenadeAI/serena) (semantic) |
| MCP server management | ✅ Multiplexer + lazy loading + idle timeout | Claude Code native MCP |
| Server-side prompt cache stability | ✅ Freezes OS output for byte-identical prefix | Anthropic API (automatic) |
| Security gate (non-OS) | ✅ Path canonicalization, keyword access control, cognitive scan | None standalone |

**ToolRecall wins when**: you run multiple agents (Hermes + Claude Code + Cursor), have 3+ MCP servers with cold-start latency, and want a single security config.

---

## How It Saves Cost — Two Mechanisms

### The Flow

```
Before ToolRecall:
  Agent → LLM says "read main.py" → subprocess fork → disk I/O → ~1.5s → result returned
  (Repeat call: same 1.5s again — every single time)

With ToolRecall (cache hit):
  Agent → LLM says "read main.py" → SQLite lookup → ~0.6ms → same result returned
  (Skip the OS entirely for repeat reads)

With ToolRecall (cache miss):
  Agent → LLM says "read main.py" → SQLite miss → subprocess fork → disk I/O → caches result → returns it
  (First call pays full price, every identical call after is ~1000× faster)
```

### 1. Local Token Reduction (~81% fewer input tokens)
Repeated tool calls served from local SQLite. In a 13-file project with 3–10× re-reads, this removes ~55–77K tokens per session. Measured hit rate: 67–97%.

### 2. Server-Side Prompt Caching Discount (up to 90%)
Anthropic and OpenAI offer up to 90% discount on input tokens that match a previous request's prefix. ToolRecall freezes OS tool outputs — every `read_file`, `hostname` returns the exact same byte string until the file changes. This makes the server-side discount **reliably available** instead of randomly busted by OS noise.

### 3. Deterministic
Byte-identical cache hits = 100% reproducible agent runs. No OS flakiness.

### 4. Safer
Zero-Trust WAF: cryptographic path resolution, `.env` air-gapping, `allow_terminal=false` drops RCE attempts.

### 5. Universal
`toolrecall mcp` works with any MCP-speaking agent.

---

## The Hourglass Architecture

```
  [ Claude Code ]   [ Cursor IDE ]   [ Hermes Agent ]
         \                |                /
          \               |               /
        +───────────────────────────────────+
        │  Standard stdio Protocol (Bridge) │  <- Client Layer
        +─────────────────┬─────────────────+
                          │ Unix Domain Socket (Linux/Mac)
                          │ TCP localhost:8567 (Windows)
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

---

## Features

### Byte-Exact Tool Caching
- **File Cache:** Invalidates on file modification (`mtime`) — no stale reads.
- **Terminal Cache:** Caches only known-static commands by TTL (hostname, whoami, pwd, uname, uptime, df, free, crontab). Dynamic commands (`git`, `ls`, `curl`) always execute live.
- **Script & Code Cache:** `cached_run`, `cached_exec` with `ttl=0` bypass for state-changing ops.
- **MCP Cache:** TTL-based caching for external MCP tool responses (~12× speedup).

### Manual Cache Refresh
- **cache_refresh_file:** Invalidate and re-read one file. Safe, no security gate.
- **bypass_cache flag:** Force fresh read on any single `cached_read` call.
- **cache_invalidate:** Clear ALL caches. Gated behind `mcp.allow_invalidate=true`.

### MCP Multiplexer (AI Gateway)
- One daemon manages all MCP servers.
- **Lazy loading:** Servers boot in 0.01s only when first called.
- **Idle timeout:** Inactive MCP subprocesses killed after 15min.
- Agents connect to **one** server: `toolrecall mcp`. Session startup: ~0.01s instead of ~1.7s.

### FTS5 Knowledge Base
Zero-dependency full-text search over docs and notes. BM25 ranking, Porter stemming. No embeddings, no GPU, no API calls.

### Data Engine (RLHF / SFT Trajectories)
```bash
toolrecall export-dataset ~/trajectories.jsonl
```
Exact (Action → State) pairs from agent sessions. Zero-cost SFT/DPO dataset generation.

---

## Configuration

TOML (default, via stdlib `tomllib`) or YAML (optional, requires `pyyaml`).

```toml
[mcp]
allowed_paths = ["~/.toolrecall"]  # Home NOT allowed by default
allow_terminal = false
default_ttl = 60

[mcp_multiplex]
enabled = true
idle_minutes = 15

[security]
tool_access_control = false
dangerous_tool_keywords = []
```

`TOOLRECALL_*` environment variables override TOML.

---

## Hermes Auto-Cache (Hermes Agent only)

```bash
git clone https://github.com/whiskybeer/toolrecall.git
cd toolrecall
bash scripts/setup.sh
```

---

## Uninstall

```bash
pip uninstall toolrecall
python3 scripts/uninstall.py --force
```

Removes: daemon, systemd service, config, cache DB, logs.

---

## Platform Support

| Platform | Transport | Status | VS Code Extension |
|----------|-----------|--------|-------------------|
| **Linux** | Unix Domain Sockets | ✅ Tested in CI (176/176 pass) | ✅ Works with ToolRecall daemon |
| **macOS** | Unix Domain Sockets | ✅ Should work (POSIX). Not in CI. | ✅ Works |
| **Windows** | TCP localhost:8568 fallback | ⚠️ Core + transport tested. CLI and extension work. | ✅ Binary auto-detected (`.exe`/`.cmd`) |

---

## Roadmap

- 🟡 **VS Code Extension** (experimental) — transparent file-read caching
- 🟡 **Browser Extension** (experimental) — page content caching for LLM agents
- Live cache dashboard (`toolrecall dashboard`)
- Tool-calling profiler (latency breakdown per MCP call)
- Active cache invalidation on mutation tools
- Container sandbox for `cached_run` (Docker backend)
- Webhook-triggered invalidation

## Documentation

- [ToolRecall vs Claude Code](docs/VS_CLAUDE_CODE.md) — detailed caching comparison
- [The Bottleneck Solved](docs/BOTTLENECK_SOLVED.md) — O(N²) context theory
- [Knowledge DB](docs/KNOWLEDGE_DB.md) — FTS5 indexing guide
- [Docker Deployment](docs/DOCKER.md) — containerized stack
- [Security Architecture](SECURITY.md) — WAF details, trust boundary, known limitations
- [Troubleshooting](docs/TROUBLESHOOTING.md) — common fixes