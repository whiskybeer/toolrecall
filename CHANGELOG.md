# Changelog

## [0.8.14] — 2026-07-19

### Added
- **Forward proxy auth-based routing** — detects upstream provider from API key prefix in the `Authorization` header. `Bearer sk-or-*` → `openrouter.ai`, `Bearer sk-ant-*` → `api.anthropic.com`, `Bearer xai-*` → `api.x.ai`. All three override path-based routing for any path.
- **OpenRouter path rewrite** — proxy rewrites `/v1/...` → `/api/v1/...` when routing to `openrouter.ai` (OpenRouter's API lives at `/api/v1`, not `/v1`).
- **Content-Length on proxy responses** — `resp.read()` handles upstream chunked encoding, but the response was sent without `Content-Length` and with `Connection: keep-alive`. Clients hung forever waiting for EOF. Now every non-streaming response includes `Content-Length`.
- **Agent-agnostic env vars** — `OPENAI_BASE_URL=http://localhost:8569/v1` and `ANTHROPIC_BASE_URL=http://localhost:8569` added to `~/.profile` and `~/.bashrc`. Any agent reading these routes through the proxy automatically.
- **Hermes config integration** — `model.base_url: http://localhost:8569/v1` set in Hermes config. New Hermes sessions route through the proxy by default.

### Changed
- `toolrecall/proxy.py`: header-based tiebreaker replaced with general auth-based routing (key prefix detection). Old Anthropic-only check (`x-api-key`, `anthropic-version`) kept as fallback.

### Fixed
- **Proxy 401 on OpenRouter requests** — path routing sent all `/v1/chat/completions` to `api.openai.com`. OpenRouter keys now correctly route to `openrouter.ai`.
- **Proxy 404 on OpenRouter requests** — path `/v1/chat/completions` forwarded as-is, but OpenRouter expects `/api/v1/chat/completions`.
- **Proxy timeout on all requests** — responses lacked `Content-Length`, causing HTTP/1.0 clients (Python `http.client`, some SDKs) to hang indefinitely.

### Documentation
- `docs/FORWARD_PROXY.md` — fully rewritten with auth routing table, path rewrite table, agent-agnostic setup, provider list with routing method.

---

## [0.8.13] — 2026-07-16

### Added
- **Storage backend refactor** — backend code extracted from `_db.py` to `toolrecall/storage/` package. `sqlite.py` (stdlib, default) and `libsql.py` (optional extra) behind an `open_backend(cfg)` factory. The daemon and cache modules contain zero backend-specific imports.
- **`tr turso` subcommand** — `init`, `enable`, `disable`, `status` for Turso Cloud setup via the Platform REST API (no Turso CLI binary required). Sync stays off by default (`sync_enabled = false`). Tokens default to expiring (30d). Config files written with 0600 perms.
- **daemon sync worker** — background thread calls `db_sync()` on the shared singleton connection, with exponential backoff on failure. Never opens a separate file handle.
- **Documentation:**
  - `docs/ARCHITECTURE.md` — new §5b with storage-backend-layer mermaid diagram + design-decisions table
  - `docs/LIBSQL_COMPARISON.md` — backend comparison, selection flow, security section on what sync uploads
  - `docs/ARCHITECTURE_DIAGRAM.md` — cache storage label updated
  - `SECURITY.md` — §6.3 updated: "Never by default" with Turso caveat
  - `docs/CONFIG_REFERENCE.md` — all [storage] keys documented

### Changed
- **`_db.py`** — slimmed from ~600 to 353 lines. Singleton/RLock/blocklist stays; backend code delegates to `toolrecall.storage` with backward-compat re-exports.
- **`daemon.py`** — sync worker uses `storage.sync_configured()` instead of three inline backend-specific conditions.
- **`cache.py`** — `get_stats()` delegates to `storage.stats_info()`.
- **`pyproject.toml`** — `libsql` extra pinned `libsql-experimental>=0.0.55,<0.1`.
- **config.toml** — full security warning restored in ASCII.

### Changed
- **README repositioning** — from speed-first to determinism-first. Problem statement leads with MCP sprawl, unrepeatable runs, API costs, no sandboxing. Feature priority table ranked by defensible value (MCP Multiplexer #1, Replay #2, Proxy #3, Security Gate #4, Caching #5-6). Quickstart now leads with MCP Bridge (was buried). Replay Mode promoted to its own section with CI example.
- **`DEFAULT_CACHEABLE` trimmed** — removed `ls`, `cat`, `head`, `tail`, `wc`, `grep`, `rg`, `find`, `fd`, `git status`, `git diff`, `git log`, `ps`, `du`, `df`, `date`, `cal`, `which`, `python3 --version`, `node --version`, `pip list`. Only 8 static commands remain: `hostname`, `whoami`, `pwd`, `uname -a`, `uptime`, `free -h`, `df -h /`, `crontab -l`. Matches documented README contract.
- **`cached_terminal` ttl=0 bypass** — ported from `cached_mcp_check` logic. `ttl=0` now skips cache lookup and storage entirely.
- **Cognitive scan scoped to MCP args only** — removed from `_handle_write` and `_handle_patch` handlers. File content scanning was scope creep; the scan was designed for MCP tool arguments per SECURITY.md.
- **Version bump** — 0.8.12 → 0.8.13.
- **Benchmark provenance** — labeled with actual version it ran on (v0.8.8+), not v0.8.12. README benchmark section now includes caveat that numbers were measured with original `DEFAULT_CACHEABLE`.
- **Proxy threading** — replaced single-threaded `HTTPServer` with `ThreadedHTTPServer` (ThreadingMixIn). One streaming request no longer blocks all other proxy traffic.
- **`cmd_serve`** — now checks if daemon is running before binding. Prints message and returns early when daemon manages the proxy, preventing EADDRINUSE.
- **AGENT_COMPATIBILITY.md** — rewritten with decision-table framing (agent per row, integration layer per column). Claude Code section updated: no longer warns about stale-state risk from file caching (dynamic commands are un-cached, writes fail-closed). Recommends forward proxy + multiplex-only.
- **`ctx_dropped_tokens`** — `get_stats()` now returns only confirmed cumulative total from `get_dirty()` calls, not inflated by pending tokens. Double-counting regression fixed.

### Fixed
- **Daemon shutdown zombie** — `os.kill(os.getpid(), SIGTERM)` replaces `sys.exit(0)` in daemon thread. `sys.exit()` in a non-main thread only kills the thread, leaving the process as an orphan.
- **Client write/patch fallback** — `cached_write` and `cached_patch` now fail closed when daemon is unavailable (consistent with `cached_terminal`). Previously bypassed the path allowlist.
- **`normalize_json`/`normalize_tool_args`/`normalize_command` lazy import** — replaced `locals()[name]` (raises `KeyError`) with explicit `_alias_map` dict (raises `AttributeError` as expected). Added `invalidate_file` and `refresh_file` to `__all__`.
- **`docs_get_page` argument swap** — daemon.py called `_docs_get_page(source, path)` but `docs.py` defines `(path, source)`. Fixed all call sites + client.py signature.
- **`docs_get_page` literal `\n` bug** — exact-match branch used `\n` (escaped backslash-n) instead of actual newlines.
- **Proxy Content-Encoding** — `Accept-Encoding` stripped from outgoing requests; `Content-Encoding` stripped from stored headers. Prevents gzipped responses being stored as corrupted UTF-8.
- **Proxy routing specificity** — `/v1beta` (Google) checked before `/v1`; `/v1/messages` (Anthropic) checked before `/v1/chat/completions` (OpenAI). Ordered tuple list replaces unordered dict iteration.

### Security
- **Fail-closed write/patch** — client refuses write operations when daemon is unreachable, enforcing the daemon's path allowlist as single source of truth.
- **Daemon shutdown** — `os.kill(SIGTERM)` triggers the registered signal handler which does proper cleanup (multiplexer, socket, PID file) before exiting.

### Documentation
- README: proxy disclaimer — X-Target-Host header needed for DeepSeek, xAI, Mistral, Groq, Together, OpenRouter (path routing can't distinguish OpenAI-compatible providers).
- README: scrubbed strategy-doc voice — removed "strategic error", "(the wedge feature)", "Competition" column (→ "When you need it"), "Three docs sections confirm this behavior".
- README: removed phantom `caching = false` claim (config key doesn't exist).
- README: `"1 tick instead of 4"` restored alongside `"warm daemon"` framing.
- README: fabricated cost numbers ($4.20 → $0.31) removed.
- README: shim marked experimental, moved to Layer 3 under Agent Integration.
- Benchmarks now caveated with original DEFAULT_CACHEABLE scope.
- `docs/BENCHMARK.md` version label corrected to v0.8.8+.
- Various docs: version bumps (v0.8.10 → v0.8.12), CONTEXT_TRACKER.md mermaid cleanup, removed 'Files to Create/Modify' planning sections.

## [0.8.12] — 2026-07-15

### Added
- **ctx_dropped_tokens metric** — Context Tracker now estimates tokens dropped from LLM context. When `get_dirty()` or `get_hint()` returns clean files, their size is estimated (file bytes / 4) and accumulated. Exposed in `toolrecall daemon --status`, ping response, and `context_get_stats`.
- **3 new tests** for `ctx_dropped_tokens` (tracking, accumulation, reset).
- **Healthcheck reports ctx_dropped** — hourly healthcheck output now includes `ctx_dropped=N` showing estimated tokens the agent saved by dropping clean files.

### Changed
- `context_tracker.py`: `get_stats()` no longer calls `get_dirty()` internally to avoid double-counting `ctx_dropped_tokens`.

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