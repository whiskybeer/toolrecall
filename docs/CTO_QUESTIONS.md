# ToolRecall: CTO Questions & Resolutions

A pragmatic, engineering-first response to enterprise concerns about deterministic tool caching.

---

## 1. Cache Invalidation & State Drift

**Concern:** mtime-based invalidation fails on ephemeral filesystems (Docker, NFS, EFS), non-file tool outputs (DB queries, API calls), and hidden dependencies (import chains).

**Resolution:**

ToolRecall runs as a **local daemon on the developer's workstation**, not on NFS mounts or shared filesystems. The mtime check is against the local `stat()` call. If mtime is unreliable (Docker ephemeral FS), the cache misses and executes live — the fallback is always the real OS.

**Non-file tool outputs are never cached by default.** The terminal cache allowlist contains only 8 commands:

| Command | TTL | Why it's safe |
|---|---|---|
| `hostname` | 1h | Never changes mid-session |
| `whoami` | 1h | Never changes |
| `pwd` | 1h | Never changes |
| `uname -a` | 1h | Never changes |
| `crontab -l` | 1h | Only changes on manual edit |
| `df -h /` | 5min | Changes very slowly (GB scale) |
| `uptime` | 5min | Load average, coarse health check |
| `free -h` | 5min | Capacity check, not exact values |

No `git`, no `curl`, no `psql`, no `kubectl`. Every other command executes live. If you explicitly enable TTL caching for a dynamic command, that's a config choice — the default is safe.

**Hidden dependencies (import chains):** This isn't a cache problem — it's a design choice about what you cache. ToolRecall caches exact tool outputs, not semantic state. If the agent reads `main.py` and `utils.py` changes, the agent requests `main.py` again when it actually needs it. The cache doesn't guess dependencies — that's an LLM-level problem.

---

## 2. Race Conditions & Multi-Agent Concurrency

**Concern:** Interleaved writes/reads produce corrupted state; parallel agent paths create split-brain context with stale cache.

**Resolution:**

**Interleaved writes/reads** — This race exists at the OS level whether ToolRecall is present or not. `open()` + `read()` is not atomic. ToolRecall does not introduce this — it inherits it from the OS. The same race would occur without caching.

**Split-brain context** — If an agent forks parallel paths, one modifying state and the other reading cached data, that's a bug in the agent orchestration, not the cache. ToolRecall returns what was asked for. If the orchestrator doesn't invalidate after mutations, the problem is upstream.

Active cache invalidation on mutation tools is on the roadmap. The `mcp_toolrecall_cache_refresh_file` tool already exists for manual invalidation.

---

## 3. Security, Isolation & Data Leakage

**Concern:** Cross-tenant cache contamination on shared runners; cache poisoning via SQLite DB tampering.

**Resolution:**

**Cross-tenant contamination** — This is the strongest objection. If you run ToolRecall on a shared CI/CD runner where Agent A and Agent B share a filesystem and a cache DB, Agent B could receive Agent A's cached data if the path and arguments match exactly.

**Mitigation:** ToolRecall's WAF enforces path allowlists. For production CI, use per-job cache isolation via `TOOLRECALL_CACHE_DB` environment variable, or disable the daemon entirely. ToolRecall is designed for **developer workstations**, not shared build infrastructure. This architectural boundary should be documented clearly.

**Cache poisoning** — If someone can write to `~/.toolrecall/cache.db`, they already have filesystem access. At that point your agent is compromised regardless of caching. The cache file is a local SQLite file with the same permissions as the user's home directory — it does not create a new attack surface.

**Privilege escalation** — ToolRecall runs as the same user as the agent. It does not require root. It does not open network ports (Unix Domain Sockets only on Linux/Mac). It does not execute code — only returns cached bytes.

---

## 4. Architectural Fragility (The "Black Box")

**Concern:** Caching obscures audit trails; Python runtime updates break the interception layer.

**Resolution:**

**Obfuscated debugging** — Every cached response includes `{"cached": true}` in the MCP response. The HTTP proxy mode adds a `X-ToolRecall-Cache: HIT` header. If your observability pipeline parses tool responses, this data is available. The response is identical to a live execution — only the `cached` flag differs. This is the same contract as any other caching layer (Redis, CDN, browser cache).

**Python runtime updates cannot break ToolRecall** — ToolRecall does not monkey-patch anything. It does not use:

- `LD_PRELOAD` (no shared library hooking)
- Python import hooks (no `sys.meta_path` manipulation)
- Subprocess interception (no `os.exec*` wrapping)
- Signal handlers or ptrace

It is a **standard MCP server** that listens on a Unix Domain Socket. The agent explicitly calls `toolrecall cached_read` instead of `read_file`. This is the same architecture as any other tool server — Claude Code's native MCP, GitHub MCP, Postgres MCP. A Python runtime update cannot affect it because ToolRecall operates at the MCP protocol layer, not the OS primitive layer.

In the OSI model: ToolRecall sits at Layer 5 (Session/IPC), not Layer 3 (OS System Calls). It does not intercept — it *replaces* the downstream call with a cache lookup when a match exists.

---

## 5. Architecture vs Alternatives

| Approach | Complexity | Dependencies | Cache Control |
|---|---|---|---|
| ToolRecall (OS-level cache) | 76KB, zero deps | Python 3.11+ stdlib | mtime + TTL, configurable |
| Stateful context management | High (LangGraph, TaskWeaver) | Heavy framework | Semantic, lossy |
| Application-aware caching (git hashes) | Medium | Custom per-tool | Manual, requires agent rules |
| OS-noise stripping for native prompt caching | Low-Medium | Custom MCP wrapper | No local speedup, only API discount |

**ToolRecall wins on:** install size, zero dependencies, deterministic replay, no LLM in the caching loop, sub-millisecond latency, built-in WAF.

**ToolRecall loses on:** cross-session memory (it's not a memory system), shared-infrastructure deployment (it's for workstations), non-deterministic tool outputs (those execute live by design).

---

## Architectural Boundary

ToolRecall is designed for:

- **Developer workstations** — local agent sessions on the engineer's machine
- **Single-user daemon** — one agent, one cache, one user
- **Read-heavy workloads** — file reads, static OS queries, deterministic tool outputs

ToolRecall is not designed for:

- **Shared CI/CD runners** — use `TOOLRECALL_CACHE_DB` isolation or disable
- **Cross-session memory** — use claude-mem or similar for that
- **Write-heavy orchestration** — mutation tools bypass cache by default

The security model is: **same user, same machine, same trust boundary as the agent itself.**
