# ToolRecall — The Deterministic Tool Cache for LLM Agents

**No LLM decides what to cache. No second agent. No misclassification. Only you do.**

ToolRecall is a **deterministic** middleware layer for autonomous AI agents. It sits between the agent and the OS, catching tool executions and managing MCP servers via Unix Domain Sockets.

Unlike caching frameworks that use a second LLM ("Cache Planner") to classify tools as cacheable or not — introducing hallucination risk, extra API cost, and cold-start latency — ToolRecall is purely deterministic: files invalidate on mtime, commands expire by explicit TTL, and `ttl=0` guarantees a tool **always** executes live. No guesses. No grey zones. No data loss from a bad LLM classification.

| What ToolRecall IS | What ToolRecall IS NOT |
|---|---|
| ✅ **Deterministic** — byte-exact tool output cache from SQLite, no LLM in the caching loop | ❌ Not an LLM-driven Cache Planner — no second agent deciding what to cache |
| ✅ **MCP Multiplexer** — single daemon manages all external MCP servers | ❌ Not a chronological call-graph — mtime handles staleness without state tracking |
| ✅ **Zero-Trust WAF** — path sandboxing, secret air-gapping, read-only mode | ❌ Not a vector database — no embeddings, no GPU, no semantic search |
| ✅ **FTS5 Knowledge Base** — zero-dep full-text search over docs and notes | ❌ Not a distributed cache — single-node SQLite, no Redis/Cluster |
| ✅ **Deterministic replay** — freeze OS state for 100% reproducible agent runs | ❌ Not a replacement for real-time data — use `ttl=0` for dynamic endpoints |

---

## Why Not an LLM-Powered Cache?

Some caching frameworks use a second LLM — a "Cache Planner" — to classify tools by cacheability: STATIC (cache forever), TRANSIENT (expire by TTL), or NONE (never cache). That sounds intelligent, but introduces failure modes ToolRecall eliminates by design — because ToolRecall is **deterministic**, not heuristic:

| Failure mode | LLM-Driven Cache | ToolRecall (Deterministic) |
|---|---|---|
| **Misclassification** | LLM guesses `send_message()` is STATIC → messages silently dropped | `ttl=0` means NEVER cache. Binary, deterministic, no AI middleman. |
| **Extra API cost** | Every new tool needs an LLM call to classify | $0 — SQLite FTS5, no embeddings, no API calls |
| **Cold-start latency** | Must analyze tool metadata before first cache decision | First call executes live, cached on return — zero overhead |
| **Side-effect blindness** | Relies on tool name/description text, not actual behavior | mtime-based auto-invalidation — file edited? next read is fresh. |
| **Reproducibility** | Non-deterministic — LLM may classify same tool differently on different runs | Always byte-identical for same args + same mtime. 100% reproducible. |

**The principle:** *Intelligent caching doesn't need an intelligence. It needs a filesystem, a clock, and the honesty to say "I don't know — execute it live."*

If you want an LLM to decide what to cache, you're adding a second agent that can hallucinate, costs money per decision, and can silently break your workflow. ToolRecall caches yes/no based on explicit TTLs and file modification times. **Deterministic by default.**

---

## The Core Problem: The Context Snowball

LLM context windows are stateless. Every time an agent reads a 10,000-token file, those 10,000 tokens enter the history. Over 100 turns, that's 1,000,000 billed input tokens for the same file — the O(N²) context snowball.

**ToolRecall's solution (Micro-RAG):**
1. Agents read the file once.
2. The agent drops the dump from its active context window.
3. Hours later if needed again, ToolRecall serves the exact bytes from SQLite — 1.5ms, no API call.
4. File edited? `mtime` invalidates the entry. Next read is fresh.

**The paradigm shift:** Cost and latency are eliminated from sessions. The *only* reason to end a session now is attention degradation (topic drift), not token bills or wait time.

---

## Universal Agent Compatibility (Drop-In MCP)

ToolRecall exposes a standard `stdio` MCP interface (`toolrecall mcp`). It works out-of-the-box with **any** agent — Claude Code, Cursor, Cline, Hermes:

```bash
claude mcp add toolrecall toolrecall mcp
```

No custom plugins. No SDK changes. 100% Day-1 ecosystem penetration.

---

## Security Architecture (The WAF)

ToolRecall doesn't cure an LLM of being prompt-injected — it cages the agent to neutralize the consequences:

- **Daemon-based IPC:** Unix Domain Sockets only. No open TCP ports (immune to SSRF).
- **Cryptographic path resolution:** `os.path.realpath` blocks `../../../etc/shadow` before the OS is touched.
- **Execution blackholes:** `allow_terminal = false` drops RCE attempts into a void.
- **Air-gapped secrets:** API keys in `~/.toolrecall/.env` — the LLM never sees them.
- **Read-only sandbox:** `read_only_sandbox = true` drops any tool containing `write`, `delete`, `push`.

---

## The Five Axes (Breaking the Iron Triangle)

1. **Faster:** Tool execution drops from ~1.5s to <0.1ms on cache hits — ~85 minutes saved in a 13-hour session.
2. **Cheaper:** Deterministic byte-exact responses qualify for 90% server-side prompt caching discount. 81% fewer input tokens.
3. **Deterministic:** Freeze OS state. 100% reproducible agent runs. No OS flakiness, no network jitter.
4. **Safer:** Zero-Trust WAF, path sandboxing, secret air-gapping.
5. **Universal:** Standard `stdio` MCP — any agent, any framework.

---

## The Hourglass Architecture

```
  [ Claude Code ]   [ Cursor IDE ]   [ Hermes Agent ]
         \                |                /
          \               |               /
        +───────────────────────────────────+
        │  Standard stdio Protocol (Bridge) │  <- Client Layer
        +─────────────────┬─────────────────+
                          │ Unix Domain Socket
        +─────────────────▼─────────────────+
        │         ToolRecall Daemon         │  <- Gateway Layer
        │  ┌─────────────────────────────┐  │
        │  │   In-Memory LRU (L1 Cache)  │  │
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
- **Terminal Cache:** Caches read-only commands by TTL (`git status` for 30s, `hostname` for 1h).
- **Script & Code Cache:** `cached_run`, `cached_exec` with explicit `ttl=0` bypass for state-changing operations.
- **MCP Cache:** TTL-based caching for external MCP tool responses (13.5× speedup measured).

### MCP Multiplexer (AI Gateway)
- One daemon manages all your MCP servers (GitHub, Brave Search, time, fetch, ...).
- **Lazy loading:** Servers boot in 0.01s only when first called.
- **Idle timeout:** Killed after 15min inactivity — daemon drops from 130MB to 11MB RAM.
- Agents connect to **one** server: `toolrecall mcp`. Session startup: ~0.01s instead of ~1.7s.

### FTS5 Knowledge Base
Zero-dependency full-text search over docs, notes, Hermes memory, Obsidian vaults. BM25 ranking, Porter stemming, source-filtered queries. No embeddings, no GPU, no API calls.

### Data Engine (RLHF / SFT Trajectories)
```bash
toolrecall export-dataset ~/trajectories.jsonl
```
Exact (Action → State) pairs mined from agent sessions. Zero-cost SFT/DPO dataset generation.

---

## Quickstart

**Requirements:** Python 3.11+, standard SQLite.

```bash
# 1. Install
pip install toolrecall

# 2. Init config + .env
toolrecall init

# 3. Start daemon
toolrecall daemon &
```

### Claude Code
```bash
claude mcp add toolrecall toolrecall mcp
```

### Direct Python
```python
from toolrecall import cached_read

result = cached_read("README.md")
print(f"Cached: {result['cached']}")
```

---

## Configuration

TOML (default, zero deps via stdlib `tomllib`) or YAML (optional, requires `pyyaml`).

```toml
[mcp]
allowed_paths = ["~/projects", "~/.hermes/skills"]
allow_terminal = false
default_ttl = 60

[mcp_multiplex]
enabled = true
idle_minutes = 15

[mcp_multiplex.servers_config]
github = { command = "npx", args = ["-y", "@modelcontextprotocol/server-github"], ttl = 60 }
```

`TOOLRECALL_*` environment variables override TOML (for CI/CD, multi-agent setups).

---

## Status

**Experimental.** Used in heavy autonomous agent workflows. Before production CI/CD: ensure your allowlist is strictly scoped.

---

## Roadmap

- Live cache dashboard (`toolrecall dashboard`)
- Tool-calling profiler (latency breakdown per MCP call)
- Active cache invalidation on mutation tools (write_file, POST, git push)
- Container sandbox for `cached_run` (Docker backend)
- Webhook-triggered invalidation (CI/events POST to purge keys)

---

## Documentation

- [The Bottleneck Solved](docs/BOTTLENECK_SOLVED.md) — O(N²) context theory
- [Knowledge DB](docs/KNOWLEDGE_DB.md) — FTS5 indexing guide
- [Docker Deployment](docs/DOCKER.md) — containerized stack
- [Security Architecture](SECURITY.md) — WAF details
- [Enterprise Scale](docs/ENTERPRISE_SCALE.md) — L1 cache metaphor
- [Troubleshooting](docs/TROUBLESHOOTING.md) — common fixes