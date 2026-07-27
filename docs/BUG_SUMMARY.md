# Bug Summary — v0.8.16 (Windows 11 / Linux cross-platform audit)

**Audited by:** [External contributor, July 2026]
**Scope:** MCP bridge instructions, config propagation, daemon lifecycle, allowlist code
**Severity key:** P1 = functional defect, P2 = hardening/design gap, P3 = portability/trivial
**Status:** All code defects fixed in this bundle; documentation gaps closed

---

## P1 — MCP Bridge Instructions Block Rendered As Python Source

**File:** `toolrecall/mcp_bridge.py:365-382`
**Status:** ✅ Fixed

Three independent defects in one string-concat block. Rebuilt as f-strings with real `\n` newlines:

| # | Defect | Fix |
|---|--------|-----|
| (a) | Plain string literals with `{...}` conditionals not evaluated | f-prefix added — conditionals now evaluate to `ENABLED`/`DISABLED` |
| (b) | `\\\\n` instead of `\n` — double-escaped newlines | Real `\n` newlines produce proper line breaks |
| (c) | 52-space indentation (cosmetic) | Normal indentation matching enclosing dict |

---

## P1 — CLI: `toolrecall daemon stop` Performs As `start`, Refusing Duplicate

**Status:** ✅ Fixed

`cmd_daemon()` checked only `"--stop"` in `sys.argv` (flag with dashes). `toolrecall daemon stop` passed `"stop"` as a bare positional — the flag check missed it, the `else` branch called `run_daemon()` which detected the running daemon and exited.

**Fix:** Accept both `--stop` (flag) and `stop` (positional) for all three daemon subcommands (stop, status, foreground).

---

## P2 — Allowlist Enforcement Duplicated Between Daemon And Client Fallback

**Files:** `toolrecall/daemon.py:136-144`, `toolrecall/client.py:134-141`
**Status:** ✅ Fixed

Both had independent copies of `realpath + startswith(allowed + os.sep)`.

**Fix:** Extracted shared `check_path_allowed(path, allowed_paths) -> bool` to new `toolrecall/path_utils.py`. Both `SecurityGate.check_read_path()` and `client.cached_read()` fallback now call it. Applys `os.path.normcase()` for Windows case-insensitive comparison.

---

## P2 — Daemon Auto-Starts Silently On `toolrecall mcp`

**Files:** `toolrecall/cli.py:590+`
**Status:** ✅ Fixed (documentation)

The `_ensure_daemon()` function auto-starts the daemon when a command requires one. Previously silent.

**Fix:** Added a user-visible stderr message when auto-starting: `[ToolRecall] Daemon not running — auto-starting silently in background...`. Added docstring note that auto-start is silent and `toolrecall daemon` can be used for explicit start.

---

## P2 — Env Vars `TOOLRECALL_MCP_ALLOWED_PATHS` / `TOOLRECALL_CACHE_DB` Silently Ignored By MCP Bridge

**File:** `toolrecall/mcp_bridge.py:main()`
**Status:** ✅ Fixed (warning)

The bridge delegates config to the daemon. Env vars set at bridge launch time had no effect — the daemon's startup config was used silently.

**Fix:** The MCP bridge now checks for active `TOOLRECALL_*` env vars on startup and prints a warning for each one found: `⚠ TOOLRECALL_MCP_ALLOWED_PATHS=... — set at bridge launch, but config is from the daemon. Restart the daemon with this env var set, or set it in your config.toml.`

---

## P2 — Daemon Config Is Sticky; Reconfiguring Requires Force-Kill

**Status:** Architectural — mitigated by `daemon stop` fix

Previously the `stop` fix (P1 above) was needed for the basic workflow: set new env vars → stop daemon → start daemon. Now `toolrecall daemon stop && toolrecall daemon` works correctly.

The deeper architectural issue (daemon never re-reads config without restart) remains, but is a feature (stability) rather than a defect.

---

## P3 — Allowlist Is Case-Sensitive On Windows (False Denial)

**Files:** `toolrecall/path_utils.py:check_path_allowed()`
**Status:** ✅ Fixed

The shared `check_path_allowed()` in `path_utils.py` applies `os.path.normcase()` to both `abs_path` and `allowed_abs` before comparing. This fixes the false-denial on Windows where `C:\\Users\\robin\\projects` couldn't match `C:\\USERS\\ROBIN\\PROJECTS\\x.py`.

---

## P3 — `MAX_PATH_LENGTH = 4096` Comment Wrong On Windows

**File:** `toolrecall/daemon.py:113`
**Status:** ✅ Fixed

Comment updated to: `# POSIX PATH_MAX (260 on Windows without long-path support; 4096 is safe on both)`

---

## Documentation: `configs/claude-code.json` Implies Context Tracker Benefit In Append-Only Harnesses

**Status:** ✅ Fixed

Changes:
- `configs/README.md` — new file explaining the Context Tracker limitation for append-only harnesses
- `docs/AGENT_COMPATIBILITY.md` — new "Critical: Context Tracker is Inert in Append-Only Harnesses" section at the top of Claude Code entry, with explicit bullet stating 7.4× figure does NOT transfer
- `docs/BENCHMARK.md` — added scoping note under the headline: "This benchmark runs on Hermes Agent... Append-only harnesses (Claude Code, Cursor) cannot do this"

---

## What's NOT Claimed (Verification Note)

The context tracker append-only limitation is **architectural reasoning, not a measured result**. The cheap falsification: run a two-turn session with the MCP server registered vs. without; the first-turn input-token delta is the fixed schema cost ToolRecall must overcome before it breaks even. That measurement is straightforward but hasn't been run.

---

## P2 — Systemd Restart Loop on Stale UDS Socket (v0.8.17)

**File:** `toolrecall/daemon.py:run_daemon()`
**Status:** ✅ Fixed

**Symptom:** After a clean daemon stop (e.g. `toolrecall daemon stop`), the UDS socket lingers briefly. When systemd's `Restart=always` starts a new instance, the ping test connects through the stale socket and gets a `pong` from the exiting daemon — triggering "refusing duplicate" + `sys.exit(0)`. After 3 such exits in 60 seconds, systemd's `StartLimitBurst=3` exhausts and marks the service `inactive (dead)` permanently. The forward proxy (port 8569) goes dark, and the WebUI gets `Connection error` for every API call.

**Fix:** Before exiting on duplicate, verify the responding PID is actually alive with `os.kill(pid, 0)`. If `ProcessLookupError` is raised (PED is dead), treat the socket as stale and proceed with startup instead of committing suicide.
