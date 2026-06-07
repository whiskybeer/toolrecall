# ToolRecall Daemon Architecture — Proposal

## 1. The Problem

ToolRecall previously had **three independent access paths** — each with its own caching process:

```
┌──────────────┐   ┌──────────────┐   ┌──────────────┐
│  Hermes      │   │  MCP Server  │   │  HTTP Proxy  │
│  (init)      │   │  (mcp)       │   │  (serve)     │
├──────────────┤   ├──────────────┤   ├──────────────┤
│ In-Memory    │   │ In-Memory    │   │ In-Memory    │
│ LRU (~20MB)  │   │ LRU (~20MB)  │   │ LRU (~20MB)  │
├──────────────┤   ├──────────────┤   ├──────────────┤
│ SQLite (WAL) │   │ SQLite (WAL) │   │ SQLite (WAL) │
│  cache.db    │   │  cache.db    │   │  cache.db    │
└──────────────┘   └──────────────┘   └──────────────┘
        ▲                                    
        │ same DB file, but...
        │ 
   Process Boundary ──────────────────────────
        │ 
   ❌ Everyone starts cold (empty LRU)
   ❌ Three processes = ~60MB RAM
   ❌ ~200ms Startup for MCP/HTTP
   ❌ Caches compete against each other
```

**Problem:** The three LRUs are *not synchronized*. Hermes caches file A in its LRU. The MCP Server has an empty LRU and reads file A from SQLite (7ms) — even though it could have fetched it in 0.001ms from a shared memory layer.

## 2. The Solution: One Daemon, Three Bridges

```
                    ╔════════════════════════════╗
                    ║   ToolRecall Daemon        ║
                    ║   (toolrecall daemon)      ║
                    ║                            ║
                    ║   ┌──────────────────┐     ║
                    ║   │  In-Memory LRU   │     ║
                    ║   │  (20MB, warm)    │     ║
                    ║   └────────┬─────────┘     ║
                    ║            │               ║
                    ║   ┌────────▼─────────┐     ║
                    ║   │  SQLite (WAL)    │     ║
                    ║   │  cache.db        │     ║
                    ║   └────────┬─────────┘     ║
                    ║            │               ║
                    ║   ┌────────▼─────────┐     ║
                    ║   │  IPC Server      │     ║
                    ║   │  UDS Socket      │     ║
                    ║   │  /tmp/tc.sock    │     ║
                    ║   └──────────────────┘     ║
                    ╚════════════════════════════╝
                              │ UDS
       ┌──────────────────────┼──────────────────────┐
       │                      │                      │
       ▼                      ▼                      ▼
┌──────────────┐    ┌──────────────┐    ┌──────────────┐
│ Hermes Client│    │  MCP Bridge  │    │  HTTP Bridge │
│              │    │              │    │              │
│ from         │    │ toolrecall   │    │ toolrecall   │
│ toolrecall   │    │ mcp          │    │ serve        │
│ .client      │    │              │    │              │
│ import *     │    │ stdin/stdout │    │ HTTP GET/POST│
│              │    │   → UDS      │    │   → UDS      │
└──────────────┘    └──────────────┘    └──────────────┘
```

### What exactly happens?

**Daemon** (`toolrecall daemon`):
- A Python process, started with the system (systemd user unit)
- Holds LRU + SQLite + UDS Server
- Processes requests: `{cmd: "read", path: "/x"} → {content: "...", cached: true}`
- Runs for days/weeks — Cache remains warm across sessions

**Hermes Client** (`from toolrecall.client import cached_read`):
- Instead of its own LRU + SQLite: forwards to the Daemon
- `cached_read(path)` → JSON over UDS → Daemon checks LRU → replies
- **Fallback:** If no Daemon is running, uses direct SQLite (legacy behavior)
- `hermes_init.py` becomes minimal (~20 LOC instead of 112)

**MCP Bridge** (`toolrecall mcp`):
- Starts instantly (no Python module loading necessary — only socket + json)
- Reads stdin (JSON-RPC), translates to UDS call, writes response to stdout
- **No internal logic** — just protocol translation

**HTTP Bridge** (`toolrecall serve`):
- Same principle: HTTP-Request → UDS call → HTTP-Response
- No internal SQLite, no LRU

## 3. Who is this for?

### Group A: Hermes users with ToolRecall (currently: Robin)
| Today | Daemon Architecture |
|-------|---------------------|
| hermes_init.py loads cache.py (112 LOC) | Client (20 LOC) |
| ToolRecall starts cold per Session | Cache is always warm (Daemon runs for days) |
| MCP Server needs extra RAM | MCP Bridge is <10MB |
| Hermes Restart = Cache cold | Daemon survives Hermes restarts |

**Value:** noticeable — especially on e2-medium with 4GB RAM. Start Daemon once, never think about caches again.

### Group B: Developers embedding ToolRecall in their own tools
| Today | Daemon Architecture |
|-------|---------------------|
| Must `from toolrecall import cached_read` | Can use UDS from any language (curl, nc, Go, Rust) |
| Python-only | Any language → UDS |

**Value:** ToolRecall becomes language-agnostic. A Go service can use the same cache as a Python script.

### Group C: Claude Code / Cursor / Codex Users
| Today | Daemon Architecture |
|-------|---------------------|
| MCP Server is a distinct process (200ms Startup) | MCP Bridge starts in <10ms |
| Every Claude Code run = new cold cache | Daemon runs, Cache warm |
| "I don't need it, startup is too slow" | "I'll use it because it's instantly available" |

**Value:** lowers barrier to entry. The Daemon turns ToolRecall into an "always-on" infrastructure on a machine.

### Group D: CI/CD
| Today | Daemon Architecture |
|-------|---------------------|
| Every CI Step starts its own cache | One Daemon per Build Host |
| Cache never gets warm (Steps are short) | Cache persists across Step boundaries |

**Value:** Only relevant in larger CI environments. Likely overkill for GitHub Actions.

## 4. How is this different?

### Different from Today

| Aspect | Today | Daemon | 
|--------|-------|--------|
| **Architecture** | 3 equal processes | 1 Center + 3 Bridges |
| **Cache-Sharing** | Only SQLite (7ms) | LRU + SQLite (0.001ms + 7ms) |
| **RAM** | ~60MB (3 × LRU) | ~25MB (1 × LRU + Bridges) |
| **MCP Startup** | ~200ms (uv run python -m ...) | ~5ms (Python stdio → socket) |
| **Language Binding**| Python only | Any language via UDS |
| **Fault Tolerance** | One process dies → others live | Daemon dies → all dead (requires systemd) |
| **Complexity** | 3 Modules side-by-side | 1 Core + 3 thin Bridges |

### Different from an HTTP Proxy

`toolrecall serve` (HTTP Proxy) is already a network bridge. The difference:

- **HTTP Proxy**: HTTP-REST-API, request/response, no Persistent Connection State, every request authenticates anew
- **Daemon + UDS**: Unix Domain Socket, persistent connection, ~10× faster, no network stack, only local communication
- **UDS vs HTTP**: UDS is ~0.1ms per call, HTTP localhost ~0.5ms. UDS has no port conflicts, no firewall, no auth needed (only Filesystem-Permissions)

### Different from direct Python Import

Direct import (`from toolrecall import cached_read`) is the fastest path — 0.001ms plus 0ms overhead. But: per process, no sharing.

The Daemon architecture sacrifices 0.1ms UDS overhead for a Shared Cache. In practice: 0.1ms is nothing — LLM-API calls take 3-10s.

**The question is not "faster or slower". The question is: "Are you using ToolRecall in one or multiple processes?"**

| Scenario | Optimal Path |
|----------|--------------|
| Single Process (only Hermes) | Direct Import — Daemon adds nothing |
| Multi Process (Hermes + MCP + HTTP) | Daemon — otherwise 3× RAM + 3× cold |
| CI/CD / Microservices | Daemon — otherwise never a warm cache |

## 5. Open Questions (Resolved in v0.3.0)

1. **systemd unit** — who manages the Daemon? A: Managed via user systemd (`systemctl --user`).
2. **Fallback behavior** — if Daemon dies, should cached_read fall back to direct SQLite? A: Yes, implemented in `client.py`.
3. **UDS Path** — `/tmp/toolrecall.sock` or `~/.toolrecall/toolrecall.sock`? A: `XDG_RUNTIME_DIR` (e.g., `/run/user/1000/toolrecall.sock`).
4. **Auth** — UDS has only Filesystem-Permissions (`chmod 700`). Is that enough? A: Yes, for single-user dev machines.
5. **Multiuser** — if two users on the machine use ToolRecall, do they need separate sockets? A: Yes, `XDG_RUNTIME_DIR` inherently isolates users.