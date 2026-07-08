# ToolRecall Daemon Architecture

## 1. The Problem

ToolRecall previously had **three independent access paths** — each with its own caching process:

```mermaid
flowchart LR
    subgraph Before["Before (v0.7.0–v0.7.2): Three Independent Caches"]
        H["Hermes<br/>In-Memory LRU<br/>SQLite"]
        M["MCP Server<br/>In-Memory LRU<br/>SQLite"]
        P["HTTP Proxy<br/>In-Memory LRU<br/>SQLite"]
    end
    H -. "same cache.db" .-> DB1["cache.db"]
    M -. "same cache.db" .-> DB1
    P -. "same cache.db" .-> DB1

```

**Problem:** The three LRUs are *not synchronized*. Hermes caches file A in its LRU. The MCP Server has an empty LRU and reads file A from SQLite (7ms) — even though it could have fetched it in 0.001ms from a shared memory layer.

## 2. The Solution: One Daemon, Three Bridges

```mermaid
flowchart TB
    subgraph Daemon["ToolRecall Daemon"]
        direction TB
        LRU["In-Memory LRU<br/>20MB, warm"]
        SQL["Singleton SQLite<br/>RLock-guarded<br/>WAL + cache.db"]
        IPC["IPC Server<br/>UDS Socket<br/>/run/user/$UID/tc.sock"]
    end

    subgraph Bridges["Three Bridges"]
        direction LR
        C1["Hermes Client<br/>from toolrecall.client<br/>import *"]
        C2["MCP Bridge<br/>toolrecall mcp<br/>stdio → UDS"]
        C3["HTTP Bridge<br/>toolrecall serve<br/>HTTP GET/POST → UDS"]
    end

    Daemon -- "UDS" --> Bridges

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
- Hermes integration is handled by the OS-level .pth shim (`toolrecall/shim.py`),
  not an init script — every Python process auto-caches.

**MCP Bridge** (`toolrecall mcp`):
- Starts instantly (no Python module loading necessary — only socket + json)
- Reads stdin (JSON-RPC), translates to UDS call, writes response to stdout
- **No internal logic** — just protocol translation

**HTTP Bridge** (`toolrecall serve`):
- Same principle: HTTP-Request → UDS call → HTTP-Response
- No internal SQLite, no LRU

## 3. Who is this for?

### Group A: Hermes users with ToolRecall
| Today | Daemon Architecture |
|-------|---------------------|
| Cold cache per session | Cache is always warm (Daemon runs for days) |
| MCP Server needs extra RAM | MCP Bridge is <10MB |
| Hermes Restart = Cache cold | Daemon survives Hermes restarts |

**Value:** noticeable — especially on e2-medium with 4GB RAM. Start Daemon once, never think about caches again.

### Group B: Developers embedding ToolRecall in their own tools
| Today | Daemon Architecture |
|-------|---------------------|
| Must `from toolrecall import cached_read` | Can use UDS from any language (curl, nc, Go, Rust) |
| Python-only | Any language → UDS |

**Value:** ToolRecall becomes language-agnostic. A Go service can use the same cache as a Python script.

### Group C: Claude Code / Cursor / Codex Users (⚠️ limited benefit)
| Today | Daemon Architecture |
|-------|---------------------|
| MCP Server is a distinct process (200ms Startup) | MCP Bridge starts in <10ms |
| Every Claude Code run = new cold cache | Daemon runs, Cache warm |
| "I don't need it, startup is too slow" | "I'll use it because it's instantly available" (multiplex/forward-proxy only) |

> ⚠️ Claude Code and Codex CLI have native state tracking that conflicts with file/terminal caching. Only the MCP multiplex and forward proxy are safe with these agents. See [Agent Compatibility](AGENT_COMPATIBILITY.md).

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

## 5. Singleton SQLite Connection & Thread Safety

The daemon uses a **singleton SQLite connection** wrapped in a `_DBConnection` class,
protected by a `threading.RLock()`. This design emerged from a real problem:

### Before (v0.7.0–v0.7.2): Connection-per-call

Each function opened its own SQLite connection via `_get_db()`, did work, and closed it.
With the daemon's 16-thread `ThreadPoolExecutor`, this caused:

- **"database is locked"** — multiple connections competing for WAL write-locks
- **Transaction conflicts** — `cannot start a transaction within a transaction`
- **Stats recording failures** — `_record()` used a separate persistent connection
  (`_get_stats_conn`) that never released its write-lock

### After (v0.7.3+): Singleton + RLock

```mermaid
flowchart LR
    DB["_get_db()"]
    RLock["Acquire RLock (recursive)"]
    Init["Lazy-init singleton _db_real"]
    Return["Return _DBConnection(_db_real)"]
    Work["Caller does work"]
    Close[".close() → commit + release RLock"]
    Del["__del__ → release on GC (safety net)"]

    DB --> RLock --> Init --> Return --> Work --> Close
    Work -. "on exception" .-> Del
    Close -. "on forgotten close" .-> Del

```

**Design decisions:**

| Decision | Why |
|----------|-----|
| **Singleton** (`_db_real`) | Eliminates WAL lock contention between connections |
| **RLock** not `Lock` | `_record()` / `_persist_file_to_sqlite()` are called from within `cached_read()` which already holds the lock — RLock allows re-entry |
| **`__del__` safety** | If an exception path skips `.close()`, the `_DBConnection` destructor releases the lock. Prevents deadlocked threads |
| **`close()` = commit** | Every caller pattern was `conn.close()`; we repurpose it to `commit + release` instead of actually closing the handle |
| **`_stats_conn` removed** | The old persistent stats connection held its own WAL lock. Now `_record()`/`_record_tokens_saved()` use `_get_db()` like everything else |

**Thread safety guarantees:**
- 16 daemon worker threads → serialized on RLock, no DB-level contention
- One process = one connection = zero "database is locked"
- Direct Python CLI calls (`cached_read()` from terminal) still open their own connection
  and may block — that's by design: the daemon owns the cache

## 6. OS-level Shim (4th Bridge, added in v0.7.0)

In addition to the three bridged paths, v0.7.0 introduces a **4th access path**: the OS-level shim.

```bash
toolrecall shim --install
```

This installs `tr_shim.pth` into site-packages. Every Python process that starts afterwards auto-imports `toolrecall.shim`, which monkey-patches:

- `builtins.open` → checks `cached_read` before touching disk
- `subprocess.run` → checks `cached_terminal` before forking

**Key difference from the three bridges:** The shim works at the Python interpreter level — zero agent-side configuration. Aider, Codex CLI, scripts, even Hermes itself benefit immediately after `toolrecall shim --install`.

```python
# tr_shim.pth contains one line:
import toolrecall.shim

# shim.py then:
#   builtins.open = _shim_open       # routes through cache
#   subprocess.run = _shim_run       # routes through cache
#   TOOLRECALL_SHIM_DISABLE=1  → skip shim per-process
```

### Comparison: 3 Bridges vs Shim

| Aspect | MCP / HTTP Bridge | OS-level Shim |
|--------|------------------|---------------|
| **Scope** | Agent connects explicitly | All Python processes worldwide |
| **Config** | MCP config per agent | One `toolrecall shim --install` |
| **Control** | Agent chooses to use cached tools | Transparent — agent never knows |
| **Fallback** | Native tools always available | Shim bypasses native tools |
| **Disable** | Remove from MCP config | `TOOLRECALL_SHIM_DISABLE=1` env |

1. **systemd unit** — who manages the Daemon? A: Managed via user systemd (`systemctl --user`).
2. **Fallback behavior** — if Daemon dies, should cached_read fall back to direct SQLite? A: Yes, implemented in `client.py`.
3. **UDS Path** — `/tmp/toolrecall.sock` or `~/.toolrecall/toolrecall.sock`? A: `XDG_RUNTIME_DIR` (e.g., `/run/user/1000/toolrecall.sock`).
4. **Auth** — UDS has only Filesystem-Permissions (`chmod 700`). Is that enough? A: Yes, for single-user dev machines.
5. **Multiuser** — if two users on the machine use ToolRecall, do they need separate sockets? A: Yes, `XDG_RUNTIME_DIR` inherently isolates users.