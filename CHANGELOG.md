# Changelog

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
- `index.html` — FAQ Q4 test count updated to 138+.

## [0.8.8] — 2026-07-10

- Initial public release on PyPI.