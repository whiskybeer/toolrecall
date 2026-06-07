# ToolRecall

[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)]()
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)]()
[![GitHub](https://img.shields.io/badge/github-Robin%2Ftoolrecall-181717?logo=github)]()

**Universal Tool-Output Cache for LLM Agents**

ToolRecall caches tool outputs (file reads, terminal commands, skills, scripts, code execution) in local SQLite FTS5. Drastically reduces input tokens and execution time for AI coding agents.

Works with **any agent** that can import Python or call HTTP endpoints.

## How it Works

Every tool call an agent makes (reading a file, running a command, viewing a skill) has to be re-executed every single time — even when the inputs and environment haven't changed. At ~150 tokens per tool call for the agent to just *see* the output, these redundant calls add up fast.

ToolRecall intercepts tool calls and checks a local SQLite database first:
- **Cache HIT** → output returned instantly (0.1ms), no tool executed, NO input tokens consumed. The agent only pays for its own reasoning.
- **Cache MISS** → tool executes normally, but the result is saved for next time.

Each cache entry carries an automatic invalidation strategy so it never returns stale data:

| Type | Invalidates when |
|------|-----------------|
| **file reads** | File modification time (mtime) changes |
| **terminal commands** | TTL expires (default: 30s, configurable) |
| **skill views** | Skill file mtime changes |
| **script runs** | Script file mtime + TTL |
| **code execution** | Content hash + TTL |

## Impact

| Metric | Agent without ToolRecall | Agent with ToolRecall |
|--------|------------------------|----------------------|
| Input tokens per session | ~250,000 | **~50,000 (−80%)** |
| Tool wait time per answer | ~7s | **~0.1s (−99%)** |
| Session length before compression | ~40 turns | **~120 turns (3×)** |
| File read token cost | 10,000 tokens | **~0 tokens (stat only)** |
| Terminal command token cost | 500 tokens + 30s | **0 tokens + 0.1ms** |

The agent isn't paying for tool output anymore — it's just paying for its own reasoning. That's where the real savings come from.

## Quick Install

```bash
pip install toolrecall
```

Zero external dependencies — Python stdlib only (http.server, sqlite3, tomllib, hashlib, subprocess, json).

---

## Architecture

ToolRecall has **three modes** — the cache core is always the same, only the access pattern changes:

```
                    ToolRecall Architecture

  ┌──────────────┐    ┌──────────────────┐    ┌──────────┐
  │ Python Import │    │  HTTP Proxy      │    │  CLI     │
  │ (direct)      │    │  (toolrecall     │    │ (CI/CD)  │
  │               │    │   serve)         │    │          │
  │ Hermes        │    │  ┌────────────┐  │    │ status   │
  │ Claude Code*  │    │  │ Port 8511  │  │    │ stats    │
  │ Codex*        │    │  │            │  │    │ index    │
  │ Any Python    │    │  │ /cached_*  │  │    │ nginx    │
  │ agent         │    │  │ /docs_*    │  │    └──────────┘
  └──────┬───────┘    │  │ /health    │  │
         │            │  └─────┬──────┘  │
         ▼            │        │         │
  ┌──────────────────────────────────────────┐
  │  ToolRecall Cache Core (SQLite FTS5)     │
  │  ┌──────┐ ┌──────┐ ┌──────┐ ┌───────┐  │
  │  │File  │ │Term  │ │Skill │ │Script/│  │
  │  │Cache │ │Cache │ │Cache │ │Code   │  │
  │  └──────┘ └──────┘ └──────┘ └───────┘  │
  └──────────────────────────────────────────┘
              │
              ▼
  ┌──────────────────┐    ┌────────────────┐
  │ ~/.toolrecall/   │    │ Nginx (opt.)   │
  │ cache.db         │    │ SSL terminator │
  │ knowledge.db     │    │ Port 443       │
  └──────────────────┘    └────────────────┘

  * = via HTTP proxy only (no Python import possible)
```

**Port 8511:** The HTTP proxy listens on port 8511 by default (configurable via config.toml `[proxy].port`). This port was chosen because: (1) not in the system-reserved range (< 1024) — no root needed, (2) not conflicting with common services, (3) easy to remember.

**Nginx** is recommended in front of the proxy for SSL termination + auth. The proxy itself does NOT handle SSL — it's intentionally kept dependency-free (Python stdlib only). Use `toolrecall nginx` to generate the config.

### Module Map

```
toolrecall/
├── __init__.py     # Public API exports
├── cache.py        # Core caching logic (SQLite FTS5, 5 cache types, mtime/TTL)
├── proxy.py        # HTTP proxy server (Python stdlib http.server)
├── cli.py          # CLI entry point: status, stats, invalidate, index, serve, nginx
├── config.py       # TOML config loader (search path: CWD → ~/.config → /etc → default)
├── config.toml     # Default configuration (user can override)
├── docs.py         # FTS5 full-text search engine (BM25, Porter stemming)
└── hermes_init.py  # Hermes auto-cache init script (loads on every session start)
```

---

## Setup for your Agent

### Agent that can import Python (recommended)

```python
from toolrecall import cached_read, cached_terminal, docs_search
```

Just import and use — ToolRecall works out of the box with default config.

### Hermes Agent

```bash
bash <(curl -s https://raw.githubusercontent.com/Robin/toolrecall/main/setup.sh)
```

Then restart Hermes or run `/reset`. Every session shows:
```
  ============================================
  ToolRecall Auto-Cache active
  5 cache types: file, terminal, skill, script, code
  ============================================
```

**Without the setup script:**
```bash
pip install toolrecall
hermes config set agent.init_scripts '["~/.toolrecall/hermes_init.py"]'
```

### Claude Code / Codex / Cursor / Any HTTP-capable Agent

These agents can't import Python directly, but can call HTTP endpoints:

```bash
# Start the ToolRecall proxy
toolrecall serve
# HTTP proxy on http://localhost:8511
```

Then configure your agent to use ToolRecall's endpoints:

| Endpoint | Description |
|----------|-------------|
| `GET /cached_read?path=/path/file` | Cache file reads |
| `GET /cached_terminal?cmd=git+status&ttl=30` | Cache terminal commands |
| `GET /cached_skill?name=skill-name` | Cache skill views |
| `GET /docs_search?query=keyword` | Full-text search |
| `GET /health` | Health check |

Use with `nginx` for SSL + auth: `toolrecall nginx` generates the config.

### Any Agent via HTTP

```bash
toolrecall serve
curl "http://localhost:8511/cached_read?path=README.md"
curl "http://localhost:8511/health"
```

---

## Python Usage

```python
from toolrecall import cached_read, cached_terminal
from toolrecall import cached_run, cached_exec, docs_search

# Cache file reads (mtime-based — checks mtime, no re-read if unchanged)
content = cached_read('/path/to/file.md')

# Cache terminal commands (TTL-based)
result = cached_terminal('git status', ttl=30)

# Cache script execution (mtime + TTL)
result = cached_run('analyze.py', '--input data.json', ttl=120)

# Cache Python code execution (content-hash + TTL)
stats = cached_exec('import pandas; print(df.describe())', ttl=300)

# Full-text search (BM25, no embedding needed)
info = docs_search('how does feature X work')
```

## CLI Commands

| Command | Description |
|---------|-------------|
| `toolrecall status` | Cache status and stats |
| `toolrecall stats` | Detailed stats (JSON) |
| `toolrecall invalidate` | Clear all caches |
| `toolrecall index` | Build/update knowledge database |
| `toolrecall serve` | Start HTTP proxy on port 8511 |
| `toolrecall nginx` | Generate nginx reverse proxy config |

## Safety: When NOT to cache

**`cached_run` and `cached_exec` return cached output — the script/code is NOT re-executed!**

| Operation | Safe to cache? | Use |
|-----------|---------------|-----|
| Read-only analysis | Yes | `ttl=300` |
| Report generation | Yes | `ttl=600` |
| State-changing operations | No | **`ttl=0`** (bypass cache) |
| API calls, webhooks | No | **`ttl=0`** (bypass cache) |

Rule: **If re-running would change something, set `ttl=0`.**

## Token Savings (benchmark)

| Cache Type | Without ToolRecall | With ToolRecall | Savings |
|-----------|-------------------|-----------------|---------|
| file_read (10K file) | 10,000 tokens | ~0 tokens (stat only) | **~100%** |
| terminal (30s cmd) | 500 tokens + 30s | 0 tokens + 0.1ms | **100% + 30s** |
| script run | 1000 tokens + 5s | 0 tokens + 0.1ms | **100% + 5s** |
| code exec | 200 tokens + 0.5s | 0 tokens + 0.1ms | **100% + 0.5s** |

## License

MIT
