# How ToolRecall Works — The Deterministic Cache

ToolRecall doesn't ask "does this file still exist?" on repeat reads.
**It only asks the cache: "have I seen this before?"**

## Two Cache Paths, One Daemon

ToolRecall has two independent cache layers, both served by the same daemon:

| Path | What it caches | Cache key | Invalidation | Speedup |
|------|---------------|-----------|-------------|---------|
| **MCP bridge** (tool-level) | File reads, terminal output, MCP server responses | Tool name + arguments | File mtime, TTL | **1 tick statt 4** (stat-only) |
| **Forward proxy** (API-level) | Full HTTP responses from API providers | Request body SHA256 hash | Body hash — same request = same response | **Zero tokens consumed**, provider never contacted |

Both start automatically with `toolrecall daemon`. The MCP bridge is stdio-based (agent connects as MCP client). The forward proxy listens on `:8569` — point `OPENAI_BASE_URL` / `ANTHROPIC_BASE_URL` at it.

## The Core Loop (MCP bridge — tool caching)

```
Agent: "read main.py"

ToolRecall checks:
  ┌─ In-Memory LRU hit? ──────────────────┐
  │  YES ✔️  ~0.001ms  → Return cached    │
  └────────────────────────────────────────┘
       │ NO
  ┌─ SQLite hit? ──────────────────────────┐
  │  YES ✔️  ~7ms  → Prime LRU, return    │
  └────────────────────────────────────────┘
       │ NO (miss)
  ┌─ Execute real read_file() ─────────────┐
  │  → Prime LRU + SQLite                  │
  │  → Return fresh result                 │
  └────────────────────────────────────────┘
```

**The lookup is always validated against the file's mtime.** If mtime changed,
the entry is evicted and a fresh read occurs.

**Key point:** On a cache hit, ToolRecall returns stored bytes from the last
read — it does NOT re-read the file from disk. The mtime check is a lightweight
`stat()` (~0.01ms), not a full file open.

## 1 Tick Instead of 4

A normal file read requires 4 OS operations: **stat** (check existence) → **open** (acquire handle) → **read** (transfer bytes) → **close** (release handle). Each is a kernel syscall with measurable overhead.

ToolRecall reduces this to **1 operation**: a single `stat()` to validate mtime. On cache hit, the bytes come from memory (in-memory LRU) — no open, no read, no close. The OS filesystem is fully bypassed.

For terminal commands, the savings are even larger: each `subprocess.run()` forks a child process (~1.5s) with shell setup, PATH resolution, and I/O piping. ToolRecall replaces the entire chain with a single SQLite lookup (~7ms) or memory lookup (~0.001ms).

**Impact on energy and time:**
- File read: 4 OS calls → 1 OS call (75% fewer kernel transitions)
- Terminal command: full subprocess fork → SQLite read (99.9% less CPU)
- Provider API: unique payload → deterministic byte string → 90% prefix-caching discount (no redundant LLM processing)

## The Only Tie to Reality: mtime

ToolRecall doesn't blindly trust its cache forever. On every lookup it does a
lightweight `stat()` call (~0.01ms) on the real file to check its **modification time**:

```
Cache entry has:  mtime = 2026-06-12 10:00:00
Real file has:    mtime = 2026-06-12 10:00:00  → SAME → cache hit
Real file has:    mtime = 2026-06-12 14:30:00  → DIFFERENT → cache invalidated, fresh read
```

If the mtime changed, the cache entry is invalidated and the next call goes to the OS. This prevents serving stale data without asking the OS every single time.

## What This Means

- **Repeat reads are ~1000× faster** (0.6ms cache vs 1.5s subprocess)
- **Deterministic byte strings** → the API provider sees the exact same prompt prefix every turn → **90% server-side prompt caching discount** activates
- **No "does the file exist?" check** — only "is it in the cache?" — which is the entire speed secret

## Why This Is Not Context Compression

ToolRecall caches **tool outputs**, not the agent's context window. The agent still appends every result to its prompt. What changes:

| Without ToolRecall | With ToolRecall |
|---|---|
| Each `read_file` forks a subprocess → 1.5s | Repeat reads from SQLite → 0.6ms |
| OS noise (timestamps, PIDs) changes every payload → no prompt caching discount | Byte-identical outputs → 90% discount on prefix |
| Agent must wait for disk I/O on every call | Agent gets instant response for cached results |

But the agent's context window still grows. ToolRecall is a **local execution cache**, not a context window manager.
