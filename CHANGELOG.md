# Changelog

## [0.8.11] — 2026-07-14

### Added
- **Auto-checkpoint on daemon start** — daemon calls `set_checkpoint(name="daemon_start")` at boot, so context tracker starts with checkpoint=1 instead of 0. Dirty/clean tracking is meaningful from the first tool call.
- **Context tracker stats in ping response** — `toolrecall daemon --status` and `{"cmd": "ping"}` now include `context_tracker: {checkpoint, dirty, clean, total_read}` for live monitoring.
- **7 new integration tests** for context tracker daemon integration (auto-checkpoint, read tracking, write tracking, ping stats, hint endpoint, status output).

### Fixed
- **Pipx editable install** — symlinked pipx venv `toolrecall` package to source directory, so daemon picks up code changes immediately without manual `cp`.
- **Pytest crash** — downgraded from 9.1.0 to 8.0.0 (capture plugin crash in this environment).
- **Context tracker tests** — test suite now handles shared daemon state correctly (path-based assertions instead of count-based).

### Documentation
- `docs/CLI.md` — `--remove` → `--uninstall` (matches actual CLI).
- `docs/HERMES_TRANSPARENT_CACHE.md` — `--remove` → `--uninstall` (2 occurrences).
- `docs/ARCHITECTURE.md` — `93.8% O(n²) reduction` → `~90% reduction`.
- `docs/APPENDIX.md` — `76 KB` → `~132 KB install`; `v0.6.0 roadmap` → `v0.8.10 roadmap delivered` with 7 items.
- `docs/TESTING.md` — `~330 tests (v0.7.5)` → `550+ tests across 38 files (v0.8.10)`.
- `tests/README.md` — `~150+ tests across 30 files` → `550+ tests across 38 files (v0.8.10)`.
- `docs/BENCHMARK.md` — `v0.3.0` → `v0.8.8+`.
- `docs/ARCHITECTURE_DIAGRAM.md` — `v0.7.0` → `v0.8.10`.
- `docs/AGENT_COMPATIBILITY.md` — Hermes row notes Context Tracker auto-hint.
- `README.md` — `default_ttl` under `[mcp]` → `terminal_default_ttl` under `[cache]` matching actual config structure.
- `CHANGELOG.md` — previous entry `138+` → `550+`.

## [0.8.10] — 2026-07-14

### Added
- **Context Tracker auto-hint** — daemon injects `_agent_hint` in `context_get_dirty` response with clean/dirty file lists. New `context_get_hint` daemon command for lightweight hint-only queries.
- **MCP bridge auto-trigger** — after every non-context tool call, the bridge calls `context_get_hint` and appends the hint to the tool response. Agents get context guidance on every turn without explicit tracker calls.
- `_format_context_hint()` — shared helper in daemon for emoji-coded hint generation.

### Documentation
- `docs/CONTEXT_TRACKER.md` — updated to document auto-hint, context_get_hint endpoint, and MCP bridge auto-trigger behavior.
- `README.md` — Context Tracker feature row updated to mention auto-hint.

## [0.8.9] — 2026-07-13

### Added
- **Round 2: MCP Context Tracker tools** — `context_set_checkpoint`, `context_get_dirty`, `context_get_stats`, `context_reset` exposed as MCP tools for any MCP-capable agent.
- **Round 4: Forward proxy streaming support** — detects `"stream": true` in request bodies via regex, bypasses cache, relays upstream response chunk by chunk (SSE passthrough). No buffering, no caching of streaming responses.
- `universal_newlines=True` alias for `text=True` in shim routing (old Python convention).
- Configurable upstream timeout via `TOOLRECALL_FORWARD_TIMEOUT` (default 30s) and `TOOLRECALL_FORWARD_STREAM_TIMEOUT` (default 300s) env vars.

### Fixed
- **Bug 2 (shim):** `_is_safe_string_command` was inverted — rejected `capture_output=True` (the most common agent pattern), so terminal caching never engaged. Now **requires** `capture_output=True` (or `stdout=PIPE`) AND `text=True`/`universal_newlines=True`. Rejects `cwd`/`env`/`input`/`check`. Calls without capture fall through to original `subprocess.run`.
- **Bug 1 (shim):** `_shim_open` improperly handled `OSError` re-raise — now correctly re-raises after path detection.
- **Bug 3 (shim):** `_shim_run` only returned stdout, not stderr — now includes both in `CompletedProcess`.
- **Bug 4 (shim):** `apply()` was using substring `in` check for pytest detection — now uses `os.path.basename()` to avoid false positives on scripts with "test" in their path.
- **Bug 5 (shim):** `Popen` was patched with a no-op wrapper — removed entirely.
- **Bug 6 (shim):** `tr` binary initialization could fail silently — now handled.
- **`capture_output` guard (v0.8.8 regression):** inverted logic — now correctly routes **only** when `capture_output=True` or `stdout=PIPE`, not when it's absent.
- **Stream detection (v0.8.8):** byte-literal `b'"stream": true'` missed compact JSON (`{"stream":true}`) — replaced with `rb'"stream"\s*:\s*true'` regex catching all whitespace variations.
- **Replay docstring:** falsely claimed "daemon checks Replay mode" — corrected to "planned integration".
- **CONTEXT_TRACKER.md:** "Three MCP Tools" heading listed 4 tools — fixed to "Four MCP Tools".

### Changed
- **O(n²) breakdown docs:** every-5-turns drop model replaced with **every-turn drop** model. Context oscillates between dirty-only (~15K) and full-turn (~65K). Reduction figure updated from 93.8% → ~90% (more accurate for per-turn semantics).
- **README install size:** updated from 76 KB → ~132 KB (package grew with more modules, adapters, docs).
- **CONTEXT_TRACKER.md:** Agent pattern section updated to show end-of-turn cleanup cycle.

### Documentation
- `docs/CONTEXT_TRACKER.md` — heading, O(n²) math, agent pattern, comparison table all updated.
- `toolrecall/replay.py` — docstring corrected to reflect planned (not wired) daemon integration.
- `toolrecall/adapters/herdr.py` — context tracker listing now accurate (tools are live).
- `explainer.html` — SVG chart green path changed to oscillation pattern; label `~268K flat · −93.3%` → `~65K bounded · −90%`.
- `index.html` — FAQ Q4 test count updated to 550+.

## [0.8.8] — 2026-07-10

- Initial public release on PyPI.