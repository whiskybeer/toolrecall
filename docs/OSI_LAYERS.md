# ToolRecall in the OSI Model

The OSI model is usually for networking, but agent tool execution has a similar layered stack. Here's where ToolRecall sits and what it changes at each layer.

---

## The Agent Tool Execution Stack

+-----------------------------------------------------+
|  Layer 7 -- Application (The Agent)                  |
|  Claude Code, Cursor, Hermes, Cline                 |
|  "I need to read main.py"                           |
|                                                     |
|  +---------------------------------------------+    |
|  |  Layer 6 -- MCP / Tool Protocol              |    |
|  |  stdio MCP, HTTP, custom tool interfaces    |    |
|  |  "toolrecall cached_read main.py"           |    |
|  +-------------------+-------------------------+    |
|                      |                               |
|  +-------------------v-------------------------+    |
|  |  Layer 5 -- Session / IPC                    |    |
|  |  Unix Domain Socket, TCP localhost          |    |
|  |  *** TOOLRECALL SITS HERE ***               |    |
|  +-------------------+-------------------------+    |
|                      |                               |
|  +-------------------v-------------------------+    |
|  |  Layer 4 -- Transport (The Daemon)           |    |
|  |  ToolRecall daemon:                         |    |
|  |    +- Cache check (SQLite LRU, ~0.6ms) --+ |    |
|  |    |  HIT -> return cached bytes, skip OS  | |    |
|  |    |  MISS -> forward to OS, cache result  | |    |
|  |    +--------------------------------------+ |    |
|  +-------------------+-------------------------+    |
|                      |                               |
|  +-------------------v-------------------------+    |
|  |  Layer 3 -- Network / OS System Calls        |    |
|  |  subprocess fork, docker exec,              |    |
|  |  http.client to external APIs               |    |
|  +-------------------+-------------------------+    |
|                      |                               |
|  +-------------------v-------------------------+    |
|  |  Layer 2 -- Data Link / Kernel               |    |
|  |  file system I/O, process scheduler,        |    |
|  |  TCP stack, Docker daemon IPC               |    |
|  +-------------------+-------------------------+    |
|                      |                               |
|  +-------------------v-------------------------+    |
|  |  Layer 1 -- Physical / Hardware              |    |
|  |  SSD, RAM, network card, CPU cycles         |    |
|  +---------------------------------------------+    |
+-----------------------------------------------------+

---

## What Changes Per Layer

| OSI Layer | Without ToolRecall | With ToolRecall (Hit) | What's Different |
|---|---|---|---|
| **L7 -- Agent** | Calls tool every time | Calls tool every time | **No change** -- agent is agnostic |
| **L6 -- Tool Protocol** | `cached_read main.py` | `cached_read main.py` | **No change** -- same MCP call |
| **L5 -- IPC / Session** | UDS routes to daemon | UDS routes to daemon | **No change** -- same socket |
| **L4 -- Daemon (GATE)** | Forwards to OS | **Returns from SQLite LRU** | **This is the change** -- daemon intercepts here |
| **L3 -- OS System Calls** | subprocess fork, `open()`, `read()` | **SKIPPED entirely** | **Eliminated** -- the daemon never reaches here |
| **L2 -- Kernel** | File system I/O, process scheduling | **SKIPPED** | **Eliminated** |
| **L1 -- Hardware** | CPU cycles, disk I/O | **SKIPPED** | **Eliminated** |

---

## The Key Insight

ToolRecall doesn't change **what** the agent does or **how** it speaks to tools. It inserts a **caching gate at Layer 4** that short-circuits the entire stack below it on cache hits.

**Layers 5--7 are identical with or without ToolRecall.**  
**Layers 1--3 are completely bypassed on cache hits.**

That's why it's a drop-in: the agent, the MCP protocol, and the IPC socket stay the same. Only the daemon in the middle decides "do I go to the OS or do I serve from memory?"

---

## Why This Matters for Speed

The ~1000x speedup comes from eliminating **everything below Layer 4**:

| Operation | Latency |
|---|---|
| SQLite LRU lookup (L4) | ~0.6ms |
| subprocess fork + disk I/O (L3--L1) | ~1,500ms |

That's not optimizing the subprocess -- it's **removing the subprocess entirely**. The agent never touches the OS stack on cache hits.
