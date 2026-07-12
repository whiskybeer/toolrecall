# Hermes Transparent Cache Mode

## Why "separate" is default (and why nobody notices)

ToolRecall installs via `setup.sh` or `pipx install` in **"separate" mode**:
- It registers `cached_read`, `cached_terminal` as *extra* tools alongside native ones
- Native `read_file`, `terminal` remain unchanged
- Problem: **AI agents almost never pick `cached_read`** — they default to the familiar `read_file`
- Result: cache exists, but 0-2 hits per session

That's why users see "nothing" despite ToolRecall being installed.

## What "transparent" does

Transparent caching for Hermes is provided by the **OS-level `.pth` shim**
(`toolrecall/shim.py`), not by an init script. Hermes Agent has no
`init_scripts` config key — the `hermes_init.py` mechanism that used to be
documented here never actually loaded. The shim is the agent-agnostic
mechanism that works for Hermes, Codex CLI, Aider, OpenCode, and any other
Python-based agent.

The shim monkey-patches `builtins.open` and `subprocess.run` at the Python
interpreter level. The agent still calls native tools — but the underlying
file reads and subprocess executions are served from the cache.
**The agent never notices.**

### Tools intercepted (via the shim)

| Native Call | Cache Backend | Benefit |
|-------------|---------------|---------|
| `builtins.open` (file reads) | `cached_read` | mtime-based, in-memory + SQLite |
| `subprocess.run` / `Popen` | `cached_terminal` | TTL-based, SQLite |

### Enable

```bash
toolrecall shim --install
```

This installs `tr_shim.pth` into site-packages. Every Python process that
starts afterwards auto-imports `toolrecall.shim`. No per-agent config or
`init_scripts` entry is needed — the shim is the mechanism.

### Disable

```bash
TOOLRECALL_SHIM_DISABLE=1   # per-process env var
# or
toolrecall shim --remove     # uninstall the shim entirely
```

## Risks

### 1. Cache bugs break native tools

If the cache gets corrupted (rare SQLite issues), file reads can return stale
data. Disable the shim (`TOOLRECALL_SHIM_DISABLE=1` or `toolrecall shim
--remove`) to fall back to uncached behavior.

**Recovery:** `rm ~/.toolrecall/cache.db && toolrecall daemon restart`

### 2. Stale data

If the daemon doesn't track mtime changes correctly, the shim returns stale
files. This can happen if the daemon has been running for hours and a file was
modified while the cache still holds the old hash.

**Recovery:** `toolrecall invalidate` or restart daemon.

### 3. Global scope

The shim patches `open()` and `subprocess.run()` for **every** Python process
on the machine — not just the agent. This is by design (zero agent-side
config) but means a buggy shim affects all Python scripts. Use
`TOOLRECALL_SHIM_DISABLE=1` to bypass per-process.

### 4. Infrastructure file noise

The shim intercepts **all** `open()` calls, including your agent's internal
infrastructure files (cwd trackers, env snapshots, config polls, cron job
lists). These are tiny, rewritten constantly, and never benefit from caching
— but they inflate the cache stats.

**Solution:** Configure exclude prefixes in `toolrecall.toml`:

```toml
[shim]
exclude_prefixes = [
    "/tmp/hermes-cwd-",   # Hermes terminal cwd tracker
    "/tmp/hermes-snap-",  # Hermes terminal env snapshot
]
```

Or via env var: `TOOLRECALL_SHIM_EXCLUDE_PREFIXES=/tmp/hermes-cwd-,/tmp/hermes-snap-`

Empty list = bypass NOTHING. Add your framework's internal paths as needed.

### 5. Visibility into agent behavior

Because the shim intercepts every `open()` and `subprocess.run()` call, the
ToolRecall healthcheck and stats (`toolrecall stats`) provide a real-time
dashboard of what the agent is doing:

- **Which files are being read** — the access log shows every file path with
  timestamps, hit rates, and token counts. If the agent is reading unexpected
  files (e.g. config files on every turn, transient temp files), you'll see it
  immediately.
- **Which commands are being run** — terminal commands are cached and logged,
  revealing what the agent is executing under the hood.
- **Detecting cache-bypass** — if the hit rate drops to 0% after >100 calls,
  the agent may be using native tools instead of cached paths.
- **Finding infrastructure noise** — the access log reveals which files are
  being read heavily but aren't user content. These are candidates for
  `[shim].exclude_prefixes`.

This visibility is a side effect of the shim's design, not a feature — but it
has proven invaluable for debugging agent behavior and performance tuning.

### 6. Non-Python agents

The shim patches the Python interpreter. Node.js-based agents (Claude Code,
Codex CLI as a Node binary, OpenCode) are unaffected — they use MCP
(`toolrecall mcp`) instead.

## Tradeoff summary

| Approach | Scope | Config | Risk |
|----------|-------|--------|------|
| OS-level shim | All Python processes | `toolrecall shim --install` | Global — affects every script |
| MCP tools (`cached_read`, etc.) | Agents that opt in via MCP | Per-agent MCP config | Agent must choose cached tools |
