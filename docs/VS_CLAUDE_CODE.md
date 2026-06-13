# ToolRecall vs Claude Code — Caching Comparison

Both ToolRecall and Claude Code cache file reads. But **how** and **what** they cache are fundamentally different.

---

## Quick Answer

| | Claude Code (default) | ToolRecall |
|---|---|---|
| **Cache scope** | Per-session (in-memory) | **Cross-session** (SQLite disk + in-memory LRU) |
| **What is cached** | `read_file` calls only | Files, terminal output, skills, docs search, MCP calls |
| **Persistence** | Lost when `claude` exits | Survives reboots, daemon restarts, agent switches |
| **All agents share?** | No — each CLI session is isolated | Yes — single daemon serves Hermes + Claude Code + Cursor |
| **Security** | None (OS-level only) | WAF: path allowlist, `.env` air-gap, terminal blackhole, cognitive scan |
| **Dependencies** | Node.js + 100MB+ | **Zero** — pure Python stdlib (76 KB) |
| **MCP multiplex** | Each server = new subprocess | Single daemon, lazy-load, idle timeout |
| **FTS5 search** | ❌ No | ✅ BM25 full-text search over cached docs |
| **Auto-heal** | ❌ Crash = manual restart | ✅ systemd + watchdog + IPC shutdown/restart |

---

## Detailed Feature Comparison

### 1. Cache Scope

**Claude Code:** In-memory LRU cache per CLI session. When you type `claude` in a new terminal, the cache is empty. Files you read in session A must be re-read in session B — same tokens, same API cost.

**ToolRecall:** SQLite WAL database + in-memory LRU. When you start a new agent session, cache hits from yesterday are still warm. The file hasn't changed? You pay 0 tokens.

```
Session 1: claude read file.py → miss → 10K tokens
Session 2: claude read file.py → miss → 10K tokens  ← Claude Code

Session 1: agent reads file.py → miss → 10K tokens
Session 2: agent reads file.py → HIT  → 0 tokens   ← ToolRecall
```

### 2. What Is Cached

| Data type | Claude Code | ToolRecall | ToolRecall TTL |
|-----------|-------------|------------|----------------|
| File reads (`read_file`) | ✅ In-memory per session | ✅ SQLite + LRU | Until mtime changes |
| Terminal output (`hostname`) | ❌ | ✅ | Until mtime or TTL |
| MCP server responses (GitHub issues, etc.) | ❌ | ✅ | Per-server TTL (default 60s) |
| Skill content | ❌ | ✅ | Until mtime changes |
| Docs / FTS5 search | ❌ | ✅ | BM25 indexed, no TTL needed |
| Script output (`cached_run`) | ❌ | ✅ | Configurable (0 = no cache) |
| Code execution (`cached_exec`) | ❌ | ✅ | Configurable (0 = no cache) |

### 3. Persistence

**Claude Code** holds its cache in the agent process heap. When the process exits, the heap is freed. Next session: cold cache.

**ToolRecall** stores cached data in:
1. **In-memory LRU** (~20 MB default) — sub-millisecond hot path
2. **SQLite WAL** on disk — persists across reboots, daemon restarts, agent switches

The daemon is long-lived — it starts once, survives many agent sessions. When you switch from Hermes to Claude Code to Cursor, they all hit the same warm cache.

```
Agent A reads file.py → miss → daemon caches it
Agent A exits
Agent B (different tool) reads file.py → HIT → daemon serves from SQLite
```

### 4. Security

**Claude Code** has no caching-specific security. It trusts the OS file permissions.

**ToolRecall** has a **Zero-Trust WAF** between every cached read and the agent:

```
Agent → toolrecall mcp → SecurityGate → Cache → Disk
                          ├── Path allowlist (default-deny)
                          ├── Sensitive file blocklist (.env, .ssh, .pem...)
                          ├── Path canonicalization (../../../etc/shadow → blocked)
                          ├── Null-byte rejection
                          ├── MAX_PATH guard (4096 chars)
                          └── terminal blackhole (allow_terminal=false)
```

### 5. MCP Multiplexing

**Claude Code** spawns each MCP server as a separate subprocess. Three servers = three processes. Each cold-starts at ~1.7s when the CLI starts.

**ToolRecall** manages all MCP servers from a single daemon:

- **Lazy loading:** servers start only when first called (~0.01s overhead)
- **Idle timeout:** inactive servers killed after 15 minutes
- **Single connection:** agents connect to one MCP server (`toolrecall mcp`) instead of N

```
Without ToolRecall:
  Agent starts → 3 MCP server subprocesses → ~5.1s cold start

With ToolRecall:
  Agent starts → 1 MCP connection → ~0.01s
  First tool call → lazy-loads exactly that server → ~1.7s one-time
```

### 6. Daemon Lifecycle

| Aspect | Claude Code | ToolRecall |
|--------|-------------|------------|
| Starts with | Each `claude` CLI invocation | System boot (systemd user service) |
| Stops with | CLI exits | Never (daemonized) |
| Restart on crash | Manual | Auto (systemd + watchdog) |
| Graceful shutdown | N/A | IPC shutdown/restart commands |
| Resource usage | Per-session heap | ~10 MB RSS, idle at 0% CPU |
| Ports | None (stdio only) | None (Unix Domain Socket, no open ports) |

### 7. Cross-Platform

| | Claude Code | ToolRecall |
|---|---|---|
| Linux | ✅ | ✅ (UDS) |
| macOS | ✅ | ✅ (UDS) |
| Windows | ✅ (within WSL) | ✅ (native — TCP fallback, no WSL needed) |
| VS Code extension | ❌ | ✅ (transparent file caching in editor) |
| Other IDEs | ❌ | ✅ (HTTP API — any tool can use it) |

---

## When Claude Code's Cache Is Enough

- You run **one** Claude Code session at a time
- You never re-read files across sessions
- Your agent doesn't use MCP servers with cold-start latency
- You don't need security gating on cache access
- You have <3 MCP servers

## When ToolRecall Wins

- You run **multiple agents** (Hermes + Claude Code + Cursor)
- You want **cross-session persistence** — yesterday's cache is warm today
- You have **3+ MCP servers** with noticeable cold-start latency
- You need **security** — path allowlisting, secret air-gapping, terminal blackhole
- You want **one config** for caching + security + MCP management
- You work across **Linux + Windows** and want the same tool on both

---

## Cost Comparison

Assumptions: 13-file project, 10 re-reads per session, 100 turns per session.

| | Without ToolRecall | With ToolRecall | Savings |
|---|---|---|---|
| File read tokens per session | 1,000,000 | 100,000 | **81% fewer tokens** |
| MCP server cold-start per session | 5.1s (3 servers) | 1.7s (one server) | **67% faster startup** |
| API cost per 100 sessions | ~$50 (Claude Sonnet) | ~$9.50 | **~$40 saved** |
| Cache setup time | 0 (automatic) | 0 (automatic) | — |
| Disk used | 0 (in-memory only) | ~5–50 MB (SQLite) | Negligible |

---

## Summary

**Claude Code's** built-in caching is **good enough for single-session use** — it avoids re-reading files within one CLI session.

**ToolRecall** adds cross-session persistence, security, MCP multiplexing, terminal caching, FTS5 search, and auto-healing. It's designed for **multi-agent, multi-session workflows** where a single daemon serves all tools.

> *"Claude Code caches read_file per session. ToolRecall caches everything, for all sessions, for all agents."*
