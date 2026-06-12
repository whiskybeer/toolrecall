# How ToolRecall Works — The Deterministic Cache

ToolRecall doesn't ask the OS "does this file still exist?" on repeat reads.  
**It only asks the cache: "have I seen this before?"**

## The Core Loop

```
Agent: "read main.py"

ToolRecall checks:
  ┌─ Is "read main.py" in the SQLite cache? ─┐
  │                                           │
  YES ✔️                                      NO ❌
  │                                           │
  ├─ Return cached bytes (~0.6ms)             ├─ Execute real read_file()
  │  (Zero file I/O, zero subprocess)         │  → Cache the result
  │  (Even if the file is deleted or moved)  │  → Return to agent
  └───────────────────────────────────────────┘
```

**For repeat calls, the OS and filesystem are entirely bypassed.** The file could be deleted, moved, or the disk unmounted — if the bytes are in the cache, ToolRecall returns them.

## The Only Tie to Reality: mtime

ToolRecall doesn't blindly trust its cache forever. On every lookup it does a lightweight `stat()` call (~0.01ms) on the real file to check its **modification time**:

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