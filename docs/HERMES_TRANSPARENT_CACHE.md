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

The shim monkey-patches `builtins.open` for read-only file access at the Python
interpreter level. The agent still calls native tools — but file reads are served
from the cache. **The agent never notices.**

### Tools intercepted (via the shim)

| Native Call | Cache Backend | Benefit |
|-------------|---------------|---------|
| `builtins.open` (read-only `r`/`rt`) | `cached_read` | mtime-based, in-memory + SQLite |

> **Why does the shim NOT intercept `subprocess.run` / `Popen`?** (Option B,
> **scheduled v0.8.19**; on the current **v0.8.18 the shim still intercepts
> `subprocess.run`/`Popen`** — this section describes the v0.8.19+ behavior.)
> Routing terminal commands through the daemon was **fundamentally lossy**: the
> daemon strips the config `source`/`cd`/`export`/`printf-cwd` wrapper lines and runs
> the inner command in the daemon's own working directory and environment, not the
> calling agent's. Every plain command (`pwd`, `git status`, `ls`) produced output
> from the wrong directory, which then got **cached and replayed** — surfacing as
> garbled shell output and offline-looking sessions. Terminal command output is now
> executed **natively** in the agent's process (correct cwd + env + shell state), and
> is never transparently cached at the shim layer. Commands you know are read-only
> and deterministic remain cachable *explicitly* via the daemon's `cached_terminal`.

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
toolrecall shim --uninstall     # uninstall the shim entirely
```

## Risks

### 1. Cache bugs break native tools

If the cache gets corrupted (rare SQLite issues), file reads can return stale
data. Disable the shim (`TOOLRECALL_SHIM_DISABLE=1` or `toolrecall shim
--uninstall`) to fall back to uncached behavior.

**Recovery:** `rm ~/.toolrecall/cache.db && toolrecall daemon restart`

### 2. Stale data

If the daemon doesn't track mtime changes correctly, the shim returns stale
files. This can happen if the daemon has been running for hours and a file was
modified while the cache still holds the old hash.

**Recovery:** `toolrecall invalidate` or restart daemon.

### 3. Global scope

The shim patches `open()` for **every** Python process on the machine — not just
the agent. This is by design (zero agent-side config) but means a buggy shim
affects all Python scripts that read files. Use `TOOLRECALL_SHIM_DISABLE=1` to
bypass per-process. Subprocess/terminal calls are never intercepted.

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

Because the shim intercepts every read-only `open()` call, the ToolRecall
healthcheck and stats (`toolrecall stats`) provide a real-time dashboard of
what the agent is doing:

- **Which files are being read** — the access log shows every file path with
  timestamps, hit rates, and token counts. If the agent is reading unexpected
  files (e.g. config files on every turn, transient temp files), you'll see it
  immediately.
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

## Why `mcp_cache` is 0 when the shim is active

When the shim is installed, a Python agent's file reads go **directly through
the shim to the daemon**, bypassing the MCP bridge entirely. Consequently
`mcp_hits`/`mcp_cache` stay at `0` — **by design, not a bug**. The bridge is
present but idle for file reads; `file_cache` shows the real activity. This is
normal for a shim-based agent and no action is needed. (Healthcheck: `shim=active`
with nonzero `file_cache` = working as intended. The `shim=` field distinguishes
this from the case where the shim is actually missing.)
