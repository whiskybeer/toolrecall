# Hermes Transparent Cache Mode

## Why "separate" is default (and why nobody notices)

ToolRecall installs via `setup.sh` or `pip install` in **"separate" mode**:
- It registers `cached_read`, `cached_terminal` as *extra* tools alongside native ones
- Native `read_file`, `terminal` remain unchanged
- Problem: **AI agents almost never pick `cached_read`** — they default to the familiar `read_file`
- Result: cache exists, but 0-2 hits per session

That's why users see "nothing" despite ToolRecall being installed.

## What "transparent" does

The `hermes_init.py` monkey-patches Hermes' tool registry handlers for `read_file`, `terminal`, `write_file`, `patch`, and `search_files`
at session start. The agent still calls native tools — but responses come from the cache.
**The agent never notices.**

### Tools intercepted

| Native Tool | Cache Backend | Benefit |
|-------------|---------------|---------|
| `read_file` | `cached_read` | mtime-based, in-memory + SQLite |
| `terminal` | `cached_terminal` | TTL-based, SQLite |
| `write_file` | `cached_write` | Skips write if content matches disk |
| `patch` | `cached_patch` | Skips if already applied |
| `search_files` | `cached_terminal` (TTL 60s) | Redundant I/O for repeated searches |

### Enable

```toml
# ~/.config/toolrecall/toolrecall.toml
[hermes]
transparent_cache = "transparent"
```

Then restart Hermes or `/reset`.

### Env override (no config change)

```bash
TOOLRECALL_HERMES_MODE=transparent hermes
```

### What you'll see in the startup banner

```
==================================================
  ToolRecall Caching Registered
  Tools: cached_read, cached_terminal, cached_write, cached_patch\n  Mode:  Transparent\n  + Transparent Monkey-Patches: read_file, terminal, write_file, patch, search_files
  Backend: Daemon (UDS) — shared cache
==================================================
```

If it says `Mode: Transparent` — it's working.

## Risks

### 1. Cache bugs break native tools

If the cache gets corrupted (rare SQLite issues), `read_file` breaks — not just `cached_read`.
In "separate" mode you can fall back to native tools. In "transparent" mode you can't.

**Recovery:** `rm ~/.toolrecall/cache.db && toolrecall daemon restart`

### 2. Stale data

If the daemon doesn't track mtime changes correctly, transparent mode returns stale files.
This can happen if the daemon has been running for hours and a file was modified
while the cache still holds the old hash.

**Recovery:** `toolrecall invalidate` or restart daemon.

### 3. Hermes API coupling

The patch targets `tools.registry` — an internal Hermes API. If Hermes ships an update
that changes this API, transparent mode breaks and `read_file` returns errors.

**Fix:** Remove the `[hermes]` section from config → falls back to "separate" → works again.

### 4. Hermes-only (without shim)

Transparent mode patches Hermes' Python-internal tool registry. Other agents
(Claude Code, Cursor, Cline) use MCP — they don't have this mechanism.
They always use explicit `toolrecall mcp` tools.

### Alternative: OS-level Shim (v0.7.0+)

Instead of Hermes transparent mode, you can use the **OS-level shim** which works with *any* agent:

```bash
toolrecall shim --install
```

The shim patches `open()` and `subprocess.run()` at the Python interpreter level — every Python process on the machine auto-caches. This includes Aider, Codex CLI, Claude Code, Cursor, Cline, Hermes, scripts.

**Tradeoff:**

| Approach | Scope | Config | Risk |
|----------|-------|--------|------|
| Hermes transparent mode | Hermes only | `[hermes] transparent_cache = "transparent"` | Hermes API coupling |
| OS-level Shim | All Python processes | `toolrecall shim --install` | Global — affects every script |
