# ToolRecall — The Deterministic Tool Cache for LLM Agents

**No LLM decides what to cache. No second agent. No misclassification. Only you do.**

## What Is ToolRecall?

ToolRecall is a lightning-fast, local caching layer and security guard built specifically for AI agents and LLM tool-calling workflows.

Instead of letting an AI agent waste time and money repeatedly making the exact same API calls, running the same terminal commands, or reading unchanged local files, ToolRecall instantly remembers the previous results.

- **Massive Cost Savings ($0):** Traditional caching solutions use a second LLM to "guess" whether a tool output should be cached, racking up massive API token bills. ToolRecall uses a local SQLite lookup that costs absolutely nothing.
- **Instantaneous Performance (~0.6ms):** Waiting for an LLM cache decision takes 500ms–2s. ToolRecall brings that down to <1ms, eliminating cold-start latency for AI agents.
- **Bulletproof Determinism:** AI is unpredictable. ToolRecall relies on strict systems engineering rules — file modification timestamps and user-defined TTLs — so you always get the exact data you expect.
- **Ultra-Fast Local Security:** Includes a WAF that stops path traversal attacks and dangerous terminal commands in ~7µs using high-speed regex patterns, before the command ever hits your machine.

ToolRecall behaves like a highly efficient filing cabinet, not an expensive librarian. It strips away unnecessary cloud dependencies and AI guesswork — making your developer tools faster, cheaper, and inherently secure.

ToolRecall is a **deterministic** middleware layer for autonomous AI agents. It sits between the agent and the OS, catching tool executions and managing MCP servers via Unix Domain Sockets (Linux/Mac) or TCP localhost fallback (Windows).

Unlike caching frameworks that use a second LLM ("Cache Planner") to classify tools as cacheable or not — introducing hallucination risk, extra API cost, and cold-start latency — ToolRecall is purely deterministic: files invalidate on mtime, commands expire by explicit TTL, and `ttl=0` guarantees a tool **always** executes live. No guesses. No grey zones. No data loss from a bad LLM classification.

| What ToolRecall IS | What ToolRecall IS NOT |
|---|---|
| ✅ **Deterministic** — byte-exact tool output cache from SQLite, no LLM in the caching loop | ❌ Not an LLM-driven Cache Planner — no second agent deciding what to cache |
| ✅ **MCP Multiplexer** — single daemon manages all external MCP servers | ❌ Not a chronological call-graph — mtime handles staleness without state tracking |
| ✅ **Zero-Trust WAF** — path canonicalization, secret air-gapping, read-only MCP keyword filter | ❌ Not a vector database — no embeddings, no GPU, no semantic search |
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

LLM context windows are stateless. Everything accumulates. This means two independent cost escalators:

**Level 1 — File repetition (O(N), linear):**
A 10,000-token file, read once, stays in context for 100 turns: 10K × 100 = **1,000,000 billed input tokens** for the same content. Expensive, but at least predictable.

**Level 2 — The real O(N²) snowball (quadratic):**
In reality, context grows continuously through new tool outputs — not just one file. After 100 turns it hits ~500K tokens, not 10K. And attention mechanisms scale at O(N²):

```
Context size → Attention pairs per turn
   10K     →       50 million
  100K     →      5 billion
  500K     →    250 billion   (after 100 turns without ToolRecall)
```

Every additional turn then costs 500K input tokens + 250B compute operations. The iceberg isn't the one file — it's the **accumulated garbage**.

**ToolRecall breaks both curves:**
1. **File cache** → file read once, then ~0.6ms from SQLite → 0 tokens for repeats
2. **Micro-RAG** → agent drops large outputs from active context, re-fetches byte-exact from cache on demand → context stays bounded, attention costs don't explode

Result: **81% fewer input tokens + context stays manageable + attention costs flat.**

Cost and latency per session decrease, but the LLM API call (~8-12s per turn) remains the bottleneck. The benefit is longer sessions before context compression kicks in, not free sessions.

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

- **Daemon-based IPC:** Unix Domain Sockets (Linux/Mac) or TCP localhost fallback (Windows). No open ports — immune to SSRF.
- **Cryptographic path resolution:** `os.path.realpath` blocks `../../../etc/shadow` before the OS is touched.
- **Execution blackholes:** `allow_terminal = false` drops RCE attempts into a void.
- **Air-gapped secrets:** API keys in `~/.toolrecall/.env` — the LLM never sees them.
- **Default-deny init flow:** `toolrecall init` prompts for allowed paths interactively. Without config, ALL paths are blocked until explicitly allowed.
- **MCP keyword access control:** `tool_access_control = true` blocks any MCP tool whose name contains `write`, `delete`, `push`, etc. This is a substring match on tool names — not an OS sandbox, no process isolation. A tool named `post_message` passes through even if it modifies state. Pair with Docker/gVisor for real isolation.
- **Cognitive Pre-Flight (deterministic semantic scan):** Before dispatching any MCP tool call, the daemon evaluates arguments against a library of prompt-injection patterns — regex signatures for jailbreak families, heuristic entropy scores, and keyword blacklists. Zero LLM involved. Zero dependencies. Sub-millisecond hot-path overhead. Measured against a labeled test corpus (70 injection, 50 legitimate prompts) — benchmarked in `tests/test_cognitive_scan.py`. Optional ONNX classifier (cold path) available for edge cases — still fully local, no data leaves the machine.

---

## How ToolRecall Compares

ToolRecall does **3 things in one daemon**: cache, WAF, MCP multiplex. Each individual piece has more polished alternatives — the value is having them integrated and agent-agnostic.

| Your need | ToolRecall | Alternative | Pick if |
|---|---|---|---|
| Token reduction / fewer re-reads | ✅ SQLite+in-memory cache (~0.6ms) | [RTK](https://github.com/thinkerai/rtk) (Rust, hook-based, transparent) | You use one agent and want zero config |
| Context compression | ✅ Micro-RAG (agent drops + re-fetches) | [headroom MCP](https://github.com/nicholasgriffintn/headroom) | You want LLM-guided compression, not deterministic |
| Code/doc search | ✅ FTS5 (BM25, zero deps) | [serena](https://github.com/SerenadeAI/serena) (semantic) | You need embeddings, not just keyword search |
| MCP server management | ✅ Multiplexer + lazy loading + idle timeout | Claude Code native MCP | You use only Claude Code and 1-2 servers |
| Server-side prompt cache stability | ✅ Freezes OS output for byte-identical prefix | Anthropic API (automatic) | You don't run agents long enough for OS jitter to matter |
| Security gate (non-OS) | ✅ Path canonicalization, keyword access control, cognitive scan | None standalone — glue RTK + custom scripts | You want one config for all agents |

**ToolRecall wins when**: you run multiple agents (Hermes + Claude Code + Cursor), have 3+ MCP servers with cold-start latency, and want a single security config that applies to all of them.

**RTK wins when**: you use one agent exclusively, want a transparent hook with no MCP config, and don't need the multiplexer or WAF.

---

## How It Saves Cost — Two Mechanisms

ToolRecall reduces API cost through two independent mechanisms. The second one is the larger lever.

### 1. Local Token Reduction (~81% fewer input tokens)
Repeated tool calls (file reads, terminal commands) are served from local SQLite instead of being re-sent to the LLM. In a 13-file project with 3–10× re-reads per file, this removes ~55–77K tokens from the context per session. Measured hit rate: 67–97% depending on re-read depth.

### 2. Server-Side Prompt Caching Discount (up to 90%)
Anthropic and OpenAI offer a discount of up to 90% on input tokens that match a previous request's prefix. The catch: the prefix must be **byte-identical** — any OS jitter (different timestamp, PID, ls output) busts the cache.

ToolRecall freezes OS tool outputs: every `read_file`, `git status`, and `hostname` returns the exact same byte string until the file changes or the TTL expires. This stabilizes the prompt prefix across turns, making the server-side discount **reliably available** instead of randomly busted by OS noise.

**The local token reduction saves ~$6/session. The server-side discount applies to every API call and scales with context size — it's the larger lever.**

### 3. Deterministic
Byte-identical cache hits mean 100% reproducible agent runs. No OS flakiness, no network jitter.

### 4. Safer
Zero-Trust WAF: cryptographic path resolution (`os.path.realpath`), `.env` air-gapping (the LLM never sees API keys), `allow_terminal=false` drops RCE attempts into a blackhole, and an optional MCP keyword filter blocks tool names containing `write`/`delete`/`push`.

### 5. Universal
Standard `stdio` MCP (`toolrecall mcp`). Works with Claude Code, Cursor, Cline, Hermes, Aider — any MCP-speaking agent.

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
        │  │   In-Memory LRU (Cache)  │  │
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
| **File Cache:** Invalidates on file modification (`mtime`) — no stale reads.
| **Terminal Cache:** Caches read-only commands by TTL (`git status` for 30s, `hostname` for 1h).
| **Script & Code Cache:** `cached_run`, `cached_exec` with explicit `ttl=0` bypass for state-changing operations.
| **MCP Cache:** TTL-based caching for external MCP tool responses (~12× speedup measured).


### Manual Cache Refresh
| **cache_refresh_file:** Invalidate and re-read a single file from disk. Always returns fresh data. Safe by default — no security gate needed, respects the path allowlist.
| **bypass_cache flag:** Set `bypass_cache=true` on any `cached_read` MCP call to force a fresh read, bypassing the cache for that single call.
| **cache_invalidate:** Clear ALL caches (memory + SQLite). ⚠ Gated behind `mcp.allow_invalidate=true`.

```bash
# Refresh a single file (safe, no gate needed)
toolrecall mcp  # then call: cache_refresh_file({"path": "/home/hermes/config.yaml"})

# Bypass cache on read (safe, no gate needed)
toolrecall mcp  # then call: cached_read({"path": "/home/hermes/config.yaml", "bypass_cache": true})

# Clear everything (requires allow_invalidate=true)
toolrecall mcp  # then call: cache_invalidate({})
```

### MCP Multiplexer (AI Gateway)
- One daemon manages all your MCP servers (GitHub, Brave Search, time, fetch, ...).
- **Lazy loading:** Servers boot in 0.01s only when first called.
- **Idle timeout:** Inactive MCP subprocesses killed after 15min — daemon stays at ~8-11 MB RSS; Node.js subprocesses spike to ~130 MB VSZ when active, then get cleaned up.
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

# 4. Use via MCP (Claude Code / Cursor / Cline)
toolrecall mcp
```

### Hermes Auto-Cache (Hermes Agent only)

For one-command setup, clone the repo and run the setup script locally:

```bash
git clone https://github.com/whiskybeer/toolrecall.git
cd toolrecall
bash scripts/setup.sh
```

> **Note:** The setup script is for convenience after a verified clone. `pip install toolrecall` is the recommended install path — no curl-pipe, no remote execution.

## Uninstall

Run the uninstaller to cleanly remove ToolRecall from your system:

```bash
python3 scripts/uninstall.py          # interactive
python3 scripts/uninstall.py --force  # non-interactive
```

It removes:

- Running daemon processes
- Systemd user service (`~/.config/systemd/user/toolrecall-daemon.service`)
- Data directory (`~/.toolrecall/` — cache DB, config, logs, init script)
- Hermes config references (`init_scripts`, `mcp_servers.toolrecall`)
- Sandbox config references
- Pip package (if installed)
- Hermes skills

The repo directory is preserved — you can `rm -rf ~/toolrecall` manually if desired.
Cron jobs (toolrecall-watchdog, memory-db-sync) are flagged for removal via the agent.

## Update

Auto-detect your install method and update to the latest version:

```bash
python3 scripts/update.py              # interactive
python3 scripts/update.py --force      # non-interactive
python3 scripts/update.py --check      # check version only
```

**What it does:**

| Install method | Update command | Behaviour |
|---|---|---|
| `pip install` | `pip install --upgrade toolrecall` | Standard pip upgrade |
| Local repo (git clone) | `git pull --ff-only` | Fast-forward pull |
| Unknown | Error with manual instructions | — |

It also restarts the daemon if it was running, and verifies the import works after the update.

For major version bumps (>0.x.0), consider a clean reinstall:
```bash
python3 scripts/uninstall.py --force
pip install toolrecall
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
allowed_paths = ["~/projects", "~/.toolrecall"]
allow_terminal = false
default_ttl = 60

[mcp_multiplex]
enabled = true
idle_minutes = 15

[security]
tool_access_control = false
dangerous_tool_keywords = []
```

`TOOLRECALL_*` environment variables override TOML (for CI/CD, multi-agent setups).

---

## Physical Limitations: The "L1 Cache" Metaphor

ToolRecall's in-memory LRU cache is described throughout this documentation as an **"L1 Cache"** — a metaphor for its position in the caching hierarchy (nearest to the agent, fastest tier). This is **not** a literal claim about CPU cache hardware.

**CPU L1 cache facts:**
- L1 data cache: ~32 KB per core on modern x86-64 CPUs
- LLM weights minimum (4-bit quantized 10M params): ~5 MB — ~160× larger than L1 capacity
- A 7B-parameter model in 4-bit: ~3.5 GB — the entire L1 cache across all cores of a consumer CPU (~1.5 MB aggregate) would need to be refilled ~2,300 times per single forward pass
- LLM inference is **memory-bandwidth-bound**, not latency-bound: HBM bandwidth (~3.35 TB/s on H100) is the bottleneck, not L1 latency (~1 ns)

**What ToolRecall's memory tier actually is:**
- A configurable in-memory LRU cache in **userspace heap** (default: 20 MB max)
- Persisted to **SQLite with WAL mode** on disk
- Competing with the Python process's heap, not with the CPU's L1/L2 cache hierarchy

The metaphor is useful for understanding caching topology (closest tier → agent). It does **not** imply LLM inference can run inside CPU L1 cache — that is physically impossible at any quantization level with current or near-future silicon.

---

## Status

**Experimental.** Used in heavy autonomous agent workflows. Before production CI/CD: ensure your allowlist is strictly scoped.

---

## Platform Support

| Platform | Transport | Status |
|----------|-----------|--------|
| **Linux** | Unix Domain Sockets | ✅ Tested in CI (176/176 pass) |
| **macOS** | Unix Domain Sockets | ✅ Should work (POSIX). Not in CI — manually verify before relying on it. |
| **Windows** | TCP localhost:8567 fallback | ⚠️ **Unsupported / untested.** Transport layer has TCP fallback and signal-handling adaptations, but the full test suite has never run on Windows. Expect breakage (path separators, process management, systemd setup). Community contributions welcome. |

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
- [Security Architecture](SECURITY.md) — WAF details, trust boundary, known limitations
- [Tool Comparison](README.md#how-toolrecall-compares) — ToolRecall vs RTK, headroom, serena
- [Enterprise Scale](docs/ENTERPRISE_SCALE.md) — L1 cache metaphor
- [Troubleshooting](docs/TROUBLESHOOTING.md) — common fixes