# Changelog

## [Unreleased]

### Security
- **`cached_shell_exec` now gated by `SecurityGate.check_terminal`** — the daemon's `cached_shell_exec` dispatch previously executed commands with **no** terminal allowlist check (unlike `cached_terminal`), so a client could run shell commands even with `allow_terminal=false` or outside `allowed_terminal_commands`. It now routes through `_handle_shell_exec`, which strips agent wrappers and gates the inner command identically to `_handle_terminal`. Regression tests: `tests/test_shell_exec_gate.py`.
- **Shipped `config.toml` defaults hardened** — removed broad `allowed_paths` entries `/etc` and `/dev` (now `["~"]` only) and set `allow_terminal = false` (opt-in). Also fixed a pre-existing TOML section-placement bug so `allow_terminal`, `allow_invalidate`, `emit_context_hints`, and `allowed_terminal_commands` actually parse under `[mcp]` where the code reads them.

### Added
- **`toolrecall.adapters.litellm`** — LiteLLM Gateway Hook: stubs byte-identical duplicate large text blocks per request (keep-first, `protected_tail=2`, fails-open). Measured on **10 real SWE-bench Lite instances × 8 accumulated turns (80 requests/arm, DeepSeek V4 Flash via OpenRouter, billing-verified)**: **32.3% fewer prompt tokens** (282,688 → 417,256) and **30.0% lower billed cost** ($0.0134 → $0.0191), with no prefix-cache damage (effective $/M $0.0474 vs $0.0459). Savings are a curve that grows with session length (0% turns 1–3, ~50% by turn 8).

### Changed
- **README token-savings wording corrected** — removed the synthetic harness's "73–91%" and "~97%" request-token figures (those came from a codegen loop, not real tasks). The only billing-verified figure stated is the LiteLLM Gateway Hook's **32.3%/30%** from real SWE-bench Lite. File / Terminal Cache row now describes bounded context growth qualitatively without an unverified percentage.

## [0.8.17] — 2026-07-27

### Added
- **`docs/AGENTS.md`** — agent instructions for MCP context tracker integration pattern (checkpoint → read → dirty → drop → repeat). 7.4× endurance documented with stale-file awareness.
- **`toolrecall/path_utils.py`** — shared path allowlist validation extracted from daemon + client. Single source for `check_path_allowed()`.
- **`--multiplexer-only` flag** — `toolrecall mcp --multiplexer-only` exposes only `mcp_call`/`mcp_list_servers` for agents where file caching costs more than it saves (Claude Code, Cursor).
- **`emit_context_hints` config option** — `[mcp] emit_context_hints = true` controls whether 🧹 drop-clean hints are appended after tool calls. Automatically disabled in multiplexer-only mode. Default: `true` for stateless agents.
- **`_maybe_stub_terminal` dedup** — repeated identical terminal output (git status, test runs, ls) replaced with a content-hash stub, matching the existing `_maybe_stub` pattern for file reads.
- **`StartLimitInterval` / `StartLimitBurst`** in systemd unit — stale-socket takeover retries without failing.
- **Claude Code setup prompt** — `toolrecall setup` now detects Claude Code and offers multiplexer-only install (avoids the tested 2.4× cost increase).
- **Terminal output dedup** — `_session_terminal` hash maps command string → sha256 output. Same command in same session gets a stub instead of full content.

### Changed
- **MCP tool surface reduced** — 18 → 17 tools. Context-tracker tools (context_set_checkpoint, context_get_dirty, context_get_stats, context_get_hint, context_get_stale) always visible. Original tool count adjusted from 14 to 13 (native-named aliases removed).
- **Compaction cap re-armed** — `_maybe_stub` now enforces maximum consecutive stubs before re-sending full content, preventing Claude Code's compaction blindness.
- **`tokens_saved` → `tokens_not_read_from_disk`** — metric renamed to accurately describe what it measures (disk I/O avoided = latency saved, not cost saved). `tokens_saved_adjusted` → `tokens_not_read_from_disk_adjusted`.
- **`context_tokens_saved` zeroed** when `emit_context_hints=false` — prevents reporting misleading "savings" in append-only harnesses that can't drop content.
- **Context hints gated** — `🧹 drop-clean` hints only emitted when `emit_context_hints=true`. Prevents unusable hints in append-only harnesses.
- **`configs/README.md` rewritten** — 135→30 lines, focused on non-Python agents, context tracker limitations documented.
- **`go-client/README.md`** — removed "pre-built install" section (no releases published; build from source instead).
- **AGENT_COMPATIBILITY.md restructured** — per-feature verdict table (forward proxy ✅, multiplex ✅, file cache ❌ 2.4×, context tracker ❌ inert). Claude Code section rewritten with real A/B test data.
- **README file cache qualification** — hero section now explicitly states file caching is for stateless agents. When-to-use table with "Works for" column.
- **BENCHMARK.md scope caveat** — 7.4× endurance figure explicitly qualified as Hermes-only; append-only harness caveat added.
- **robka.de → toolrecall.dev** — URL migration complete across LICENSE, Dockerfile, pyproject.toml, README, configs, docs.

### Fixed
- **Claude Code token regression** — compaction cap re-arm + bypass hint in stub prevent unbounded stub accumulation.
- **Terminal dedup stub** — `_maybe_stub_terminal` for repeated command output within a session.
- **go-client/README** — removed non-existent pre-built install instructions.
- **docs audit fixes** — CONTEXT_TRACKER.md stale `tokens_saved` metric; AGENTS.md 7× → 7.4× rounding; ARCHITECTURE.md module listing missing `path_utils.py`.
- **Symlink escape fix** — `path_utils.py` now checks the original path (before `realpath`) against allowed dirs first, so `/etc/os-release → /usr/lib/os-release` and `/usr/lib/python3.11/EXTERNALLY-MANAGED` are no longer falsely blocked as escaping the allowlist.
- **Loopback proxy HTTPS → HTTP** — forwarding to `localhost:8569` used `HTTPSConnection`, which fails with `SSL WRONG_VERSION_NUMBER` since the daemon speaks plain HTTP. Both `_forward()` and `_forward_streaming()` now detect loopback hosts (localhost, 127.0.0.1, ::1) and use `HTTPConnection`.
- **DB corruption auto-recovery** — `cache.py` adds `_ensure_db_integrity()`, called from `_record()` every 10 min via `PRAGMA integrity_check`, and `_recover_database()` which dumps recoverable content via `sqlite3 .dump`, renames the corrupt DB to `.bak`, creates a fresh DB, and re-imports the dump. `api_cache_check` also triggers recovery on a `malformed` exception.

### Documentation
- `docs/AGENT_COMPATIBILITY.md` — Hermes description clarified: ToolRecall is built *for* Hermes, not part of official Hermes.
- `docs/AGENTS.md` — new agent context tracker integration guide
- `docs/AGENT_COMPATIBILITY.md` — rewritten with Claude Code A/B test data, feature verdict table, multiplexer-only recommendation
- `docs/BENCHMARK.md` — scope caveat: 7.4× is Hermes-only, not transferable
- `docs/CONFIG_REFERENCE.md` — `emit_context_hints` added to all three reference tables
- `docs/CONTEXT_TRACKER.md` — `tokens_saved` → `tokens_not_read_from_disk`
- `README.md` — file cache qualified for stateless agents; when-to-use "Works for" column added
- `configs/README.md` — full rewrite for non-Python agent audience
- `ARCHITECTURE.md` — `path_utils.py` added to module listing

---

## [0.8.16] — 2026-07-23

### Added
- **`tokens_saved_adjusted`** — new stats metric reporting net token savings after subtracting disk I/O overhead, giving a truthful picture of cache benefit vs cost.

### Fixed
- **Benchmark silent failures** — context tracker failures during benchmark runs now surface visibly instead of passing silently, preventing misleading "all green" results when the tracker is broken.

### Changed
- **Version alignment** — `__init__.py` bumped to match `pyproject.toml` (both now 0.8.16), fixing test collection error caused by version drift.

### Documentation
- **toolrecall.dev website link** — README now features a site badge in the hero section and lists the website as the primary documentation portal.
- **Hermes shim instructions** — `AGENT_COMPATIBILITY.md` documents the `uv tool install` alternative and manual shim fallback for Hermes users.
- **Critique-driven doc fixes** — dead/broken links repaired across README, ARCHITECTURE, and CLI.md; missing CLI reference entries restored; outdated CLI command names corrected.

---

## [0.8.15] — 2026-07-22

### Added
- **context_get_stale** — `cache.context_get_stale(path)` detects provably stale content by comparing mtime + size against the cached record. Returns `True`/`False` without pulling content from disk. Enables agent-level cache awareness without a full read.
- **Direct DeepSeek API support** — `bench/agent.py` now supports `provider=deepseek` with full 1M context window via `api.deepseek.com`. Default model: `deepseek-chat`.
- **Direct Gemini API support** — `bench/agent.py` now supports `provider=gemini` via Google's OpenAI-compatible endpoint. Default model: `gemini-2.5-flash`.
- **Per-run benchmark databases** — each benchmark run gets its own `.db` file under `bench-runs/<run_id>.db`, eliminating WAL corruption from concurrent writes to a shared `benchmark.db`.
- **Honest token accounting** — `get_stats()` now exposes `tokens_saved_cumulative` (raw DB accumulator) alongside `tokens_saved` (real cumulative savings). New metrics: `cached_content_tokens` (byte sum / 4 heuristic) for file_cache capacity, `unique_files` count.
- **Agent-tool source tagging** — `cached_read` requests tagged with `source: "agent_tool"`, enabling per-source tracking of context tokens saved.
- **`toolrecall/storage/README.md`** — documents the pluggable storage backend layer: architecture, backend table (sqlite/libsql/libsql-sync), the contract for adding a new backend.

### Benchmark Fixes & Improvements
- **Benchmark write simulation** — `bench/agent.py`'s toolrecall arm now calls `cached_write()` for files in `step.writes` after the LLM responds, so the context tracker sees real dirty state. Previously all files were always "clean" (never written through the daemon), causing all file content to be stripped every turn regardless of write metadata — making the bugfix workload behave identically to the read-only review workload.
- **`context_get_dirty()` returns checkpoint-scoped results** — no-argument branch (use current checkpoint) returned ALL dirty files since `reset()` instead of only those since the current checkpoint. Unified both branches into a single filtered path. Also fixed `target` computation that silently replaced checkpoint=0 (valid post-reset ID) with the current counter.
- **Benchmark marker formatting** — correct marker tags, DB path resolution, seed isolation across runs, schema alignment.
- **Context tokens_saved inflation** — distinguished shim reads from agent-tool reads so the counter doesn't double-count.
- **Critique-driven benchmark report fixes**:
  - **Dynamic column headers** — `analyze.py` no longer hardcodes `naive|prefix|toolrecall` columns; renders whatever arms are present, preventing cost values from appearing under wrong column headers.
  - **Wilcoxon paired test** — now pairs on `(workload_id, turn_index)` instead of `(run_id, turn_index)`. Run_ids differ across arms so the old pairing always returned an empty intersection (`n=0`).
  - **Claims table auto-populated** — C1 (growth rates), C2 (ratio widening), C3 (exhaustion comparison), C4 (probe recall) filled from computed data. C5 remains placeholder pending micro-benchmark fix.
  - **Honest positioning** — moved from buried text to headline: "ToolRecall doesn't save money — it enables work that naive/prefix cannot complete."
- **`cmd_stats` returns JSON always** — CLI no longer returns a formatted string when daemon is unreachable; consistent JSON response regardless of daemon state.
- **Remove go-client/tr binary from tracking** — 3.1MB unstripped binary no longer committed to repo; replaced with build-from-source instructions.
- **Mermaid participant declarations** — added `participant` lines to all `sequenceDiagram` blocks (GitHub Mermaid requires explicit declarations when `End` is a reserved keyword).
- **Stale benchmark artifacts removed** — `BENCHMARK_REPORT.md`, `benchmark_stats.txt` cleaned from repo root.

### Changed
- **docs/BENCHMARK.md fully rewritten** — old single-session case study replaced with three-arm controlled benchmark (naive vs prefix vs toolrecall). 6 runs, 239 turns, DeepSeek V4 Flash, seed=42 interleaved. Key finding: TR survives 140 turns vs naive's 17 (7.4× longer), sends 9.5× fewer tokens at turn-matched comparison. Version caveat added: generated with v0.8.14 (review workload only — write-simulation fix in v0.8.15 does not affect read-only results).
- **Benchmark provider consolidation** — `bench/run_arm.py` provider choices expanded to `openrouter`, `anthropic`, `gemini`, `deepseek`. Default switched to `openai/gpt-4o-mini` (was `deepseek/deepseek-v4-flash`).
- **Per-run DB directory** — `bench/analyze.py`, `turnlog.py`, `interleave.py` all migrated to per-run DBs under `~/.toolrecall/bench-runs/`.
- **Workload file validation** — `tests/benchmark_workload.py` now fails fast with clear error if any source file is missing. Reduced file set: removed `docs.py`, `mcp_server.py`, `BOTTLENECK_SOLVED.md`.
- **ARCHITECTURE.md restructured** — merged architecture diagrams, added design principles section, full README restructuring. 3 plain-text sequence diagrams converted to mermaid (`sequenceDiagram` block).

### Fixed
- **Daemon forward proxy silent failure** — `except Exception: pass` in `DaemonServer.start()` now prints a visible warning when proxy fails to start (port in use, import error).

### Removed
- `tests/benchmark_mcp.py` — obsolete MCP benchmark (superseded by per-run system)
- `tests/benchmark_on2.py` — obsolete O(N²) benchmark (replaced by three-arm)
- `BENCHMARK_REPORT.md`, `benchmark_stats.txt` — stale auto-generated artifacts
- `go-client/tr` — 3.1MB unstripped binary (build instructions replace it)

### Documentation
- `docs/BENCHMARK.md` — complete rewrite with three-arm controlled benchmark data + honest positioning + version caveat
- `docs/ARCHITECTURE.md` — merged diagrams, mermaid conversion, design principles, README restructure
- `docs/README.md` — removed Turso mention from hero section
- `toolrecall/storage/README.md` — pluggable storage backend layer documentation

---

## [0.8.14] — 2026-07-19

### Added
- **MCP bridge mcp_cache tracking** — bridge tags all UDS tool calls (`read_file`, `write_file`, `patch`, `terminal`) with `mcp_origin=True`. Daemon records `mcp_cache` hit/miss stats alongside primary `file_cache`/`terminal_cache` stats. `toolrecall stats` and healthcheck output now show non-zero `mcp_hits`.
- **Forward proxy auth-based routing** — detects upstream provider from API key prefix in the `Authorization` header. `Bearer sk-or-*` → `openrouter.ai`, `Bearer sk-ant-*` → `api.anthropic.com`, `Bearer xai-*` → `api.x.ai`. All three override path-based routing for any path.
- **OpenRouter path rewrite** — proxy rewrites `/v1/...` → `/api/v1/...` when routing to `openrouter.ai` (OpenRouter's API lives at `/api/v1`, not `/v1`).
- **Content-Length on proxy responses** — `resp.read()` handles upstream chunked encoding, but the response was sent without `Content-Length` and with `Connection: keep-alive`. Clients hung forever waiting for EOF. Now every non-streaming response includes `Content-Length`.
- **Agent-agnostic env vars** — `OPENAI_BASE_URL=http://localhost:8569/v1` and `ANTHROPIC_BASE_URL=http://localhost:8569` added to `~/.profile` and `~/.bashrc`. Any agent reading these routes through the proxy automatically.
- **Hermes config integration** — `model.base_url: http://localhost:8569/v1` set in Hermes config. New Hermes sessions route through the proxy by default.

### Changed
- `toolrecall/proxy.py`: header-based tiebreaker replaced with general auth-based routing (key prefix detection). Old Anthropic-only check (`x-api-key`, `anthropic-version`) kept as fallback.

### Fixed
- **Daemon forward proxy silent failure** — when the forward proxy failed to start (port in use, import error), the `except Exception: pass` in `DaemonServer.start()` swallowed all errors silently. Now prints a visible warning with the error message so operators know why port :8569 isn't listening.
- **Proxy 401 on OpenRouter requests** — path routing sent all `/v1/chat/completions` to `api.openai.com`. OpenRouter keys now correctly route to `openrouter.ai`.
- **Proxy 404 on OpenRouter requests** — path `/v1/chat/completions` forwarded as-is, but OpenRouter expects `/api/v1/chat/completions`.
- **Proxy timeout on all requests** — responses lacked `Content-Length`, causing HTTP/1.0 clients (Python `http.client`, some SDKs) to hang indefinitely.
- **Proxy STREAM logging** — prompt token estimates and body SHA-256 hash on streamed responses, so proxy logs and CSV are useful for SSE/non-parseable responses.

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
- **`docs_get_page` literal `\\\\n` bug** — exact-match branch used `\\\\n` (escaped backslash-n) instead of actual newlines.
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

---

## [0.8.12] — 2026-07-15

### Added
- **ctx_dropped_tokens metric** — Context Tracker now estimates tokens dropped from LLM context. When `get_dirty()` or `get_hint()` returns clean files, their size is estimated (file bytes / 4) and accumulated. Exposed in `toolrecall daemon --status`, ping response, and `context_get_stats`.
- **3 new tests** for `ctx_dropped_tokens` (tracking, accumulation, reset).
- **Healthcheck reports ctx_dropped** — hourly healthcheck output now includes `ctx_dropped=N` showing estimated tokens the agent saved by dropping clean files.

### Changed
- `context_tracker.py`: `get_stats()` no longer calls `get_dirty()` internally to avoid double-counting `ctx_dropped_tokens`.

---

## [0.8.11] — 2026-07-14

### Added
- **Auto-checkpoint on daemon start** — daemon calls `set_checkpoint(name="daemon_start")` at boot, so context tracker starts with checkpoint=1 instead of 0. Dirty/clean tracking is meaningful from the first tool call.
- **Context tracker stats in ping response** — `toolrecall daemon --status` and `{"cmd": "ping"}` now include `context_tracker: {checkpoint, dirty, clean, total_read}` for live monitoring.
- **7 new integration tests** for context tracker daemon integration (auto-checkpoint, read tracking, write tracking, ping stats, hint endpoint, status output).

### Fixed
- **Pipx editable install** — symlinked pipx venv `toolrecall` package to source, so daemon picks up code changes immediately without manual `cp`.
- **Pytest crash** — downgraded from 9.1.0 to 8.0.0 (capture plugin crash in this environment).
- **Context tracker tests** — test suite now handles shared daemon state correctly (path-based assertions instead of count-based).

### Documentation
- `docs/CLI.md` — `--remove` → `--uninstall` (matches actual CLI).
- `docs/HERMES_TRANSPARENT_CACHE.md` — `--remove` → `--uninstall` (2 occurrences).
- `docs/ARCHITECTURE.md` — `93.8% O(n)² reduction` → `~90% reduction`.
- `docs/APPENDIX.md` — `76 KB` → `~132 KB install`; `v0.6.0 roadmap` → `v0.8.10 roadmap delivered` with 7 items.
- `docs/TESTING.md` — `~330 tests (v0.7.5)` → `550+ tests across 38 files (v0.8.10)`.
- `tests/README.md` — `~150+ tests across 30 files` → `550+ tests across 38 files (v0.8.10)`.
- `docs/BENCHMARK.md` — `v0.3.0` → `v0.8.8+`.
- `docs/ARCHITECTURE_DIAGRAM.md` — `v0.7.0` → `v0.8.10`.
- `docs/AGENT_COMPATIBILITY.md` — Hermes row notes Context Tracker auto-hint.
- `README.md` — `default_ttl` under `[mcp]` → `terminal_default_ttl` under `[cache]` matching actual config structure.
- `CHANGELOG.md` — previous entry `138+` → `550+`.

---

## [0.8.10] — 2026-07-14

### Added
- **Context Tracker auto-hint** — daemon injects `_agent_hint` in `context_get_dirty` response with clean/dirty file lists. New `context_get_hint` daemon command for lightweight hint-only queries.
- **MCP bridge auto-trigger** — after every non-context tool call, the bridge calls `context_get_hint` and appends the hint to the tool response. Agents get context awareness on every turn without explicit tracker calls.
- `_format_context_hint()` — shared helper in daemon for emoji-coded hint generation.

### Documentation
- `docs/CONTEXT_TRACKER.md` — updated to document auto-hint, context_get_hint endpoint, and MCP bridge auto-trigger behavior.
- `README.md` — Context Tracker feature row updated to mention auto-hint.

---

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
- **Stream detection (v0.8.8):** byte-literal `b'stream': true` missed compact JSON (`{"stream":true}`) — replaced with `rb'"stream"\s*:\s*true'` regex catching all whitespace variations.
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

---

## [0.8.8] — 2026-07-10

- Initial public release on PyPI.

---

## v0.8.7 (2026-07-12)

- **Fix:** Shim exclude prefixes now configurable via `[shim].exclude_prefixes` in `toolrecall.toml` or `TOOLRECALL_SHIM_EXCLUDE_PREFIXES` env var. Defaults skip `/tmp/hermes-cwd-*` and `/tmp/hermes-snap-*` (Hermes terminal infra files). Empty list = bypass nothing.
- **Fix:** `context_tokens_saved` column added to `cache_stats` — tracks only cache hits from agent-tool reads (`source="agent_tool"`), not from internal infrastructure reads. Separates actual LLM-context savings from general disk-read avoidance (`tokens_saved`).
- **Fix:** `client.cached_read()` sends `source="agent_tool"` to daemon so agent file reads count toward context token savings.
- **Docs:** `HERMES_TRANSPARENT_CACHE.md` — added risk section on infrastructure file noise with config examples, plus new section on visibility-into-agent-behavior side effect.
- **Docs:** `CONFIG_REFERENCE.md` — added `TOOLRECALL_SHIM_EXCLUDE_PREFIXES` to env var table.

## v0.8.6 (2026-07-09)

- **Feature:** Cache key normalizer — deterministic JSON sorting, whitespace stripping, noise key removal (timestamps, session IDs). Opt-in via `[norm].enabled = true` or `TOOLRECALL_NORM_ENABLED=true`.
- **Feature:** Replay mode — record and replay agent tool calls for deterministic, offline, zero-cost CI testing. `toolrecall replay record <name>` / `toolrecall replay replay <name>`. Scenarios export as portable JSON.
- **Feature:** Framework adapters — Google ADK (`@cached_tool` decorator), LangChain (`ToolRecallCache` BaseCache + callback handler), herdr (integration guide via `tr` binary + MCP bridge). Thin wrappers around `toolrecall.client`, no new dependencies.
- **Feature:** Go client (`tr` binary) — cached file reads, terminal commands, and status from any language. Connects to the daemon over UDS.
- **Feature:** Forward proxy — cache LLM API responses by request body hash. Set SDK base URL to `http://localhost:8569`.
- **Feature:** Native-named MCP tools — `read_file`, `write_file`, `patch`, `terminal` as aliases for `cached_read`, `cached_write`, `cached_patch`, `cached_terminal`. Agents pick these naturally.
- **Fix:** `cached_write` and `cached_patch` now invalidate `_file_cache` after writing — prevents stale reads when the shim is active and mtime resolution doesn't change on fast writes.
- **Fix:** `_db.py` singleton now detects `TOOLRECALL_CACHE_DB` env var changes and reconnects — eliminates "no such table" warnings when tests switch DB paths.
- **Fix:** `test_mcp_bridge.py` — updated tool count assertions (10→14), tool name expectations, and replaced `importlib.reload` with `unittest.mock.patch` to prevent module state corruption.
- **Fix:** `test_regression_v078_v0711.py` — updated `TestMCPCacheFS` to use native tool names (`read_file`, `terminal`, `write_file`, `patch`).
- **Feature:** `__main__.py` — `python -m toolrecall` now works (used by `_handle_restart` fallback)
- **Feature:** `cached_run` / `cached_exec` docs added — documented alongside `cached_read`/`cached_terminal`
- **Fix:** Shim-cache double-counting — `_check_cache` no longer increments hit/miss stats twice on the same lookup
- **Fix:** Test isolation — UDS path collision, MCP Cache FS daemon startup restored, graceful skip for missing `toolrecall` binary
- **Fix:** `test_context_tracker` skips gracefully when `toolrecall` binary not on PATH
- **Fix:** CORS status in `docs/APPENDIX.md` updated to reflect fixed code
- **Clean:** Removed `go-client/` empty stub, `github-auth-permanent-fix.md`, `test_mcp_transparent_cache.py`
- **Clean:** Removed dead `mcp-legacy` reference from `mcp_bridge.py` docstring
- **Clean:** Removed dead VS Code extension step from uninstaller
- **Docs:** All stale references cleaned across 10+ doc files (hooks.py/store.py, tomli-w, VS Code, google-adk dead links, ARCHITECTURE_DIAGRAM formatting, APPENDIX, AGENT_COMPATIBILITY table, MCP_MULTIPLEXER typo, KNOWLEDGE_DB ASCII→Mermaid)
- **Docs:** pipx as primary install method throughout (README, SECURITY, CLI.md, docstrings, scripts)
- **Docs:** Removed user-specific reference from ARCHITECTURE.md
- **Scripts:** `setup.sh` formatting and output wording improved
- **Scripts:** `uninstall.py` docstring updated for install method clarity

## v0.8.5 (2026-07-07)

- **Removed:** `hermes_init.py` and `init_scripts` mechanism — Hermes Agent has no `init_scripts` config key, the script was never loaded. The OS-level `.pth` shim (`toolrecall/shim.py`) is the agent-agnostic mechanism for all Python-based agents.
- **Removed:** `patch_shim.py` / `toolrecall_patch.py` — dead `PYTHONSTARTUP` mechanism, `toolrecall_patch` module never existed in the installed package.
- **Removed:** Init script references from `toolrecall setup`, all docs, and uninstaller.
- **Fixed:** Live `~/.toolrecall/config.toml` now has `allow_terminal = true` with 27 read-only regex patterns — terminal cache was blocked because the config had `false` and no allowlist. (Source config was already updated in v0.8.4, but live config was never synced.)
- **Fixed:** Terminal regex patterns now correctly match bare commands (`cat`, `grep`, `find`, etc.) — `^cat\s` didn't match `cat` without args, changed to `^cat(\s+|$)`.
- **Fixed:** `setup.sh` now detects `pipx` (preferred for CLI tools), falls back to `pip`. Hermes section installs the `.pth` shim instead of writing `hermes_init.py`.
- **Fixed:** `scripts/uninstall.py` now checks `pipx list` before `pip show` — handles both install methods.

## v0.8.4 (2026-07-07)

- **Feature:** `toolrecall setup` auto-detects Hermes Agent, Claude Code, OpenCode/Crush — writes MCP config and instruction snippets automatically (Hermes uses the OS-level `.pth` shim, no per-agent config needed)
- **Feature:** Daemon duplicate-instance guard — `run_daemon()` pings the socket before starting, refuses if daemon already responds
- **Feature:** SQLite WAL retry — `_db()` retries once on `SQLITE_BUSY` with 100ms sleep
- **Feature:** 13 E2E tests with real daemon subprocess — lifecycle, cache ops, CLI, stress (10 concurrent, 5x rapid restart), isolated temp socket + DB
- **Docs:** `docs/AGENT_COMPATIBILITY.md` — per-agent value matrix with guidance
- **Docs:** Agent config files for Cline, OpenCode, Aider, Windsurf, Continue
- **Docs:** `tests/README.md` translated to English, full 30-file overview table
- **Docs:** `configs/README.md` rewritten with per-agent sections and 7-agent compatibility table
- **Docs:** All Claude Code references caveated across docs; shim claims scoped to Python agents only
- **Config:** `[tool.pytest.ini_options]` — e2e marker added to pyproject.toml
- **Chore:** `.gitignore` — .hermes, .ruff_cache, .pytest_cache, editor swp files

## v0.8.2 (2026-07-04)

- **Feature:** `toolrecall/toml_serializer.py` — zero-dependency TOML writer (replaces `tomli-w`)
- **Feature:** 42 unit tests for TOML serializer (round-trip verified via stdlib `tomllib`)
- **Refactor:** `config.py` `save_config()` now uses built-in serializer — no `tomli-w` dependency needed
- **Refactor:** `cli.py` `cmd_init()` — default allowed paths now include `/tmp` (in addition to `~/.toolrecall`)
- **Refactor:** `cli.py` `cmd_init()` — generated config now has `allow_terminal = true` by default
- **Fix:** `cli.py` `cmd_restart()` — handles systemd exit -15 (SIGTERM) gracefully with fallback to direct daemon start
- **Docs:** All Mermaid diagrams use plain theme (no `%%{init}` blocks) for GitHub dark mode compatibility
- **Docs:** `configs/README.md` translated from German to English
- **Chore:** `pyproject.toml` v0.8.2

## v0.8.1 (2026-07-01)

- **Feature:** `mcp_fetch.py` — built-in stdlib-only HTTP Fetch MCP server (zero deps, replaces `uvx mcp-server-fetch`)
- **Feature:** `TOOLRECALL_FETCH_MAX_BYTES` env var — configurable content size limit (default 500KB, 0 = no limit)
- **Feature:** MCP Server Registry (`toolrecall/mcp_registry.py`) — auto-resolve server names to commands, no `servers_config` needed
- **Feature:** `toolrecall mcp list` — CLI subcommand to list all registered servers with their source and command
- **Refactor:** `fetch` moved from external (uvx) to built-in server in registry
- **Refactor:** `config.py` — removed `_parse_agent_mcp_servers()` (-110 LOC), auto-resolution replaces Hermes config.yaml fallback
- **Config:** `config.toml` — `servers` default changed to `["time", "github", "sequential-thinking"]`
- **Docs:** MCP_MULTIPLEXER.md — Registry tables updated, fetch is now built-in, env var documented
- **Docs:** SECURITY.md — Fetch Layer OOM mitigation documented
- **Docs:** README.md — built-in/external tables updated, config comment fixed
- **Tests:** +50 new tests (registry, config resolve, fetch env var, PID guard) — 328 total, all passing

## v0.7.2 (2026-06-30)

- **Docs:** Full architecture diagram — system (flowchart) + sequence (read/write/cache) diagrams
- **Docs:** Transport Layer section — UDS vs TCP, framed JSON protocol, TOOLRECALL_TRANSPORT override
- **Docs:** Context Tracker section in architecture diagram + feature table in README
- **Docs:** Removed duplicate deployment section, clean split: Installation vs Deployment (Production)
- **Docs:** README CLI reference — added missing `stats`, `index-memory`, `shim` commands
- **Refactor:** Removed all dashboard/proxy_router/llama_server references from docs
- **Fix:** `start_services.sh` — stale PID cleanup, port check before `--status`

## v0.7.1 (2026-06-29)

- **Feature:** Context Tracker — checkpoint-based dirty-file tracking to break O(n²) context growth (docs/CONTEXT_TRACKER.md)
- **Daemon:** +4 new IPC commands: context_set_checkpoint, context_get_dirty, context_get_stats, context_reset
- **Client:** +4 Python API functions: context_set_checkpoint(), context_get_dirty(), context_get_stats(), context_reset()
- **Daemon:** cached_write/cached_patch auto-mark files as dirty; cached_read auto-marks as read
- **Benchmark:** 93.3% O(n²) reduction confirmed — 19 tests, 277 total (0 regressions)
- **Chore:** bump v0.7.0 → v0.7.1

## v0.7.0 (2026-06-22)

- **Refactor:** Remove all Hermes-specific code — fully agent-agnostic
- **Feature:** Transparent OS-level cache shim via `.pth` file (`toolrecall shim --install`)
- **Shim:** Auto-patches `builtins.open()` + `subprocess.run()` in every Python process
- **Shim:** Zero imports needed — `.pth` loads on interpreter startup
- **Shim:** Per-process disable via `TOOLRECALL_SHIM_DISABLE=1`
- **Config:** agent_home resolution chain: `AGENT_HOME` → `HERMES_HOME` → `~/.hermes`
- **Config:** `[hermes]` section removed, `[mcp_multiplex]` is the universal config
- **Config:** Skill dirs via `TOOLRECALL_SKILL_DIRS` env or `[paths].skill_dirs` config
- **Fix:** FTS5 auto-repair — `docs_search()` detects malformed index, transparently rebuilds, retries
- **Fix:** Multi-threaded DB guard — race condition on concurrent FTS5 index rebuilds
- **Docs:** ARCHITECTURE_DIAGRAM.md — Mermaid sequence diagram (read/write cache flow)
- **Docs:** Universal MCP config snippet works with any SDK (not just OPENAI_BASE_URL)
- **Docs:** OS-level shim documented across all architecture docs

## v0.6.1 (2026-06-19)

- **Cleanup:** removed `vscode-extension/`, `mcp_server.py`, `dataset.py`, `examples/` — legacy/experimental code never used in production
- **Docs:** restructured 27→12 files. Added APPENDIX.md, MCP_MULTIPLEXER.md. Deleted stale/duplicate docs
- **Docs:** Architecture disclaimer, fixed UDS path, fixed dead link (CTO_QUESTIONS→APPENDIX), fixed ASCII table in ARCHITECTURE.md
- **Docs:** ALL docstrings translated German→English (client.py, daemon.py)
- **Docs:** dead comment in cache.py fixed (`# ── 3. Cache miss ──` → `# 2. SQLite hit`)
- **README:** rewritten — agent-agnostic, "1 Tick Instead of 4" hero, required vs optional marked, integration table (Hermes, Claude Code, Cursor, Cline), nginx labeled optional
- **Fix:** XDG_RUNTIME_DIR vs real UID mismatch — `_default_socket_path()` validates against `os.getuid()`
- **Fix:** cache blocked — `allowed_paths` package default changed from `~/.toolrecall` to `/home/hermes`
- **Fix:** `proxy.py` — `run_server()` now binds to passed `bind` arg, not hardcoded `127.0.0.1`
- **Fix:** `daemon.py` — hardcoded `v0.3.0` replaced with `from toolrecall import __version__`
- **Fix:** config.toml — `[REDACTED]` IPs replaced with `127.0.0.1`
- **Fix:** test helper `_patch_transport()` uses `set_socket_path()` instead of direct attribute patching
- **Chore:** `.gitignore` — added `vscode-extension/`
- **Tests:** 258 tests, all passing (was 275, after removing legacy code paths and their tests)

## v0.5.3 (2026-06-13)

- **Hermes:** transparent_cache=transparent now default in setup.sh
- **setup.sh:** detects Claude Code, Cursor, OpenCode, Cline — asks per-agent to write config snippets
- **OpenCode:** auto-updates opencode.json instructions
- **README:** explains why agents don't pick cached_read (training exposure, not bias/limitation)
- **README:** config snippets for all 5 supported agents
- **Docs:** docs/HERMES_TRANSPARENT_CACHE.md (DE/EN, risks, config)

## v0.5.2 (2026-06-11)

- **Security:** TOOLRECALL_ALLOW_SENSITIVE env override for _is_sensitive_path()
- **Security:** SECURITY.md — Interface Exposure & Default Transport Security
- **Cleanup:** vitest + happy-dom removed from experimental browser extension (CVE fix applied first)
- **Cleanup:** uv.lock untracked, hardcoded paths in tests/uninstallers replaced
- **Docs:** README flow diagram, elevator pitch, HOW_IT_WORKS.md, APPENDIX.md
- **Docs:** stale terminal-cache claims fixed across README and doc files

## v0.5.1 (2026-06-11)

- **Feature:** browser-extension + api-cache (experimental)
- **Cleanup:** unused imports removed across cache.py, daemon.py, client.py, proxy.py, docs.py
- **Publish:** v0.5.1 on PyPI

## v0.5.0 (2026-06-11)

- **VS Code Extension** — transparent file-read caching via ToolRecall
- **Windows Compatibility** — native TCP fallback, no WSL needed
- **Pluggable hardening** — SHA256 hash mode, shell fallback logging, env overrides
- **Daemon reliability** — fork-safe executor, silent crash fix, watchdog auto-healing, systemd service
- **CLI:** toolrecall init, toolrecall serve --help, TOOLRECALL_* env vars
- **Security:** 4 audit findings fixed (CORS, token leak, null-byte, lazy import)
- **Dead code removed:** cmd_gc(), index_hermes_memory()
- **176/176 tests passing**

## v0.4.9 (2026-06-10)

- Fix: 2 more _send → send (cached_patch, docs_get_page)

## v0.4.8 (2026-06-10)

- Fix: 7x _send → send rename, tokens_saved keyerror
- Fix: nginx is optional, README cleanup

## v0.4.7 (2026-06-10)

- **Zero deps:** pip install toolrecall adds nothing but toolrecall
- README rewrite, log banner fix, minimal allowed_paths

## v0.4.6 (2026-06-10)

- **Agent-agnostic defaults:** no Hermes paths in config.toml
- macOS ready, platform support table in README

## v0.4.5 (2026-06-10)

- GitHub MCP opt-in, tool_access_control default empty
- Request logging

## v0.4.4 — skipped (version bump)

## v0.4.3 (2026-06-09)

- Version bump (0.4.0 → 0.4.3), deprecation cleanup
- README URL fix, "What Is ToolRecall?" section added

## v0.4.2 (2026-06-09)

- Rename: Sandbox WAF → MCP Keyword Filter
- Fix doc exaggeration

## v0.4.1 (2026-06-09)

- Uninstaller, update script
- refresh_file + bypass_cache for cached_read

## v0.4.0 (2026-06-09)

- **Initial public release**
- MCP multiplexer, FTS5 knowledge base, zero-trust WAF
- Hermes init_script integration (separate mode)
- Daemon with SQLite + In-Memory LRU
- HTTP proxy (forward + bridge)
- Security audit: WAF, path canonicalization, sensitive file blocklist
- 155 tests
