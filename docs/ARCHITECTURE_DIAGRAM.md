# ToolRecall Caching Architecture

> **Based on:** v0.7.0 (Commit `8757694`)
> **Status:** 28 June 2026

## Overview

ToolRecall is an OS-level transparent cache. It intercepts file I/O at two levels:

| Path | What it caches | Mechanism |
|------|---------------|-----------|
| **MCP bridge** | File reads, terminal output, MCP server responses | Agent connects as MCP client → daemon caches via LRU + SQLite |
| **Forward proxy** | Full HTTP API responses | Body hash → cache hit = zero tokens consumed, provider never contacted |
| **OS-level shim** (`.pth` patch) | `open()` + `subprocess.run()` everywhere | `toolrecall shim --install` → every Python process auto-caches |

All three paths share one daemon with one LRU + one SQLite store.

---

## System Architecture

```mermaid
flowchart TB
    subgraph User
        U["👤 End User"]
    end
    subgraph AgentSystem
        A["🤖 LLM Agent<br/>(Aider - Codex - Hermes)"]
        CT["📋 Context Tracker"]
        S["⚡ ToolRecall Shim"]
        SQ["💾 SQLite Cache"]
    end
    subgraph ModelSystem
        M["🧠 LLM Model"]
    end
    subgraph OSLayer
        O["📁 OS / Filesystem<br/>Disk - Network - Terminal"]
    end

    U -->|Prompt| A
    A -->|Response| U
    A -->|Inference| M
    M -->|Generated Output| A
    A -->|Can I drop file?| CT
    CT -->|Yes(clean) · No(dirty)| A
    A -->|open · read · write| S
    S -->|Lookup · Store · Invalidate| SQ
    S -->|Native I-O| O
    O -->|File content| S
```

---

## Sequence Diagram

```mermaid
sequenceDiagram
    participant User as End User
    participant Agent as LLM Agent (Aider)
    participant Model as LLM Model
    participant CT as Context Tracker
    participant Shim as ToolRecall Shim
    participant SQLite as SQLite Cache
    participant OS as Local OS/Disk

    rect rgb(25, 35, 25)
    Note over User, OS: 📖 SCENARIO 1a — Read (Cache Hit) — 0 tokens for file
    User->>Agent: Prompt requiring file.py
    Agent->>Shim: open("file.py", "r")
    Shim->>SQLite: Hash(path) → Lookup
    SQLite-->>Shim: Return Cached Content
    Shim-->>Agent: Return Content (zero tokens, no disk I/O)
    Agent->>Model: LLM Inference (file already in prompt context)
    Model-->>Agent: Generated Response
    Agent-->>User: Final Answer
    end

    rect rgb(35, 25, 25)
    Note over User, OS: 📖 SCENARIO 1b — Read (Cache Miss) — file size in tokens
    User->>Agent: Prompt requiring file.py
    Agent->>Shim: open("file.py", "r")
    Shim->>SQLite: Hash(path) → Lookup
    SQLite-->>Shim: Cache Miss
    Shim->>OS: Execute native open()
    OS-->>Shim: Return File Content
    Shim->>SQLite: Store(hash, content)
    Shim-->>Agent: Return Content
    Note over Agent, Model: File added to prompt context = file_size × ~0.25 tokens
    Agent->>Model: LLM Inference (file bytes in context)
    Model-->>Agent: Generated Response
    Agent-->>User: Final Answer
    end

    rect rgb(25, 25, 35)
    Note over User, OS: ✏️ SCENARIO 2 — File Write — file size in tokens
    User->>Agent: Prompt to edit file.py
    Agent->>Shim: open("file.py", "w")
    Shim->>OS: Execute native write to disk
    OS-->>Shim: Success
    Shim->>SQLite: Invalidate(hash)
    Shim-->>Agent: Return Success
    Note over Agent, Model: Agent has new file bytes in context = file_size × ~0.25 tokens
    Agent->>Model: LLM Inference (modified file in context)
    Model-->>Agent: Generated Response
    Agent-->>User: Final Answer
    end
```

---

## Token Cost per Scenario

| Scenario | File in Context? | Token Cost | Latency |
|----------|-----------------|------------|---------|
| **Read (Cache Hit)** | Already in context from before | **0 tokens** for the file | ~0.6ms (LRU) / ~7ms (SQLite) |
| **Read (Cache Miss)** | Must be loaded into prompt | **file_size × ~0.25 tokens** | File I/O + inference time |
| **Write** | New bytes held in prompt | **file_size × ~0.25 tokens** | Write + inference time |

> **Note:** The agent may **drop** clean files from context via the Context Tracker. On re-read those incur a Miss cost again — but the data comes from SQLite (~7ms), not from disk.

---

## The Core Principle

**Man-in-the-Middle** for file I/O:

| Operation | Path | Effect |
|-----------|---|--------|
| Read (Hit) | Agent → Shim → SQLite → Agent | No disk I/O, 0 tokens |
| Read (Miss) | Agent → Shim → OS → SQLite → Agent | Disk I/O, then cached |
| Write | Agent → Shim → OS → SQLite(Invalidate) → Agent | Disk written, cache cleared |

The contract: **after every write, the cached entry for that file is invalidated** — never stale, never inconsistent.

---

## How the Context Tracker Works

The Context Tracker runs inside the agent's process and tracks which files the agent has read vs. modified:

| File Status | Meaning | Can drop from context? |
|-------------|---------|----------------------|
| **Clean** | Agent read the file but never wrote to it | ✅ Yes — re-read from SQLite (~7ms) |
| **Dirty** | Agent wrote to the file | ❌ No — edits must stay in context |
| **Unknown** | File not yet tracked | N/A — first read will cache it |

When the agent needs to free context window space, it asks the Context Tracker for candidates. Only **clean** files are dropped — dirty files (the agent's own edits) remain in context to prevent loss.

---

## Shim Override: `TOOLRECALL_SHIM_DISABLE`

| Env Variable | Default | Effect |
|-------------|---------|--------|
| `TOOLRECALL_SHIM_DISABLE` | *(not set, shim active)* | When set to `1`, the OS-level shim (`.pth` patch) skips all caching. File operations proceed without interception — useful for debugging, benchmarking without ToolRecall, or processes that should never be cached (e.g. CI runners, build scripts with unique outputs per run). |

Without `TOOLRECALL_SHIM_DISABLE`, every `open()` and `subprocess.run()` in every Python process on the machine is transparently cached. Set it to `1` for a specific process:
```bash
TOOLRECALL_SHIM_DISABLE=1 python my_uncached_script.py
```

---

## Key Files

- `toolrecall/shim.py` — the OS-level patch module (patches `builtins.open` + `subprocess.run`)
- `toolrecall/tr_shim.pth` — `.pth` file auto-imported by site-packages (runs `import toolrecall.shim`)
- `toolrecall/hooks.py` — hook logic for open/subprocess interception
- `toolrecall/store.py` — KV-Store (SQLite FTS5 + in-memory LRU)
- `toolrecall/client.py` — Python client (used by MCP bridge + direct imports)
- `toolrecall/context_tracker.py` — Context Tracker (tracks dirty/clean file state)

---

## Installation

```bash
# MCP bridge (any agent)
toolrecall daemon &                  # Start daemon
# Add to MCP config:
# { "mcpServers": { "toolrecall": { "command": "toolrecall", "args": ["mcp"] } } }

# Forward proxy (API-level, auto-started with daemon)
export OPENAI_BASE_URL=http://localhost:8569/v1   # Any OpenAI-compatible SDK

# OS-level shim (patches every Python process system-wide)
toolrecall shim --install
```

## Env Overrides

| Env | Default | Effect |
|-----|---------|--------|
| `TOOLRECALL_SHIM_DISABLE=1` | *(not set)* | Disable the OS-level shim at runtime — skip caching for a specific process |
| `TOOLRECALL_HERMES_MODE=transparent` | *(not set)* | Enable Hermes transparent mode (hermes_init.py bypass) |