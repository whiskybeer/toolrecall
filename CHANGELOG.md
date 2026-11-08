# Changelog

## [Unreleased]

### Added
- **OpenEvolve LLM-judge memoization (`wrap_llm_feedback`, `admit_unique`).**
  Memoizes OpenEvolve's `use_llm_feedback` judge in the work ledger, closing the
  re-billing gap where evaluator memo hits still called the external LLM judge
  for every child. Includes `admit_unique` population admission guard to skip
  embedding and novelty checks on byte-identical island duplicates.
- **Per-agent cache statistics tracking (`agent_cache_stats`, `toolrecall agent`).**
  Tracks cache hits, misses, and token savings per agent identity (e.g. `hermes`,
  `claude-code`, `antigravity`) across file, terminal, skill, and API cache.
  Agent identity is strictly stats-only metadata and never enters cache keys: all
  agents share one cache (Agent A warms the cache for Agent B). Configured via
  `TOOLRECALL_AGENT_ID` env var, `X-ToolRecall-Agent` HTTP header, or explicit
  `agent_id` parameter. Inspected via `toolrecall stats --by-agent`, `toolrecall stats --agent-id <id>`,
  and `toolrecall agent ls/show/prune`. Documented in `docs/AGENT_TRACKING.md`.
- **`claude-code` canonicalization profile for the forward proxy.** Claude Code
  always streams, so without a profile its requests were relayed but never
  cached (0 hits). The new profile drops the per-run `metadata` (session id)
  from the cache key and replaces test-runner durations with a placeholder.
  On 8 headless repeats of a toy task: −62% usage vs the cold run (generic
  `hermes` profile: −24%). On 2 real SWE-bench Lite tasks (Sonnet, 5 runs each,
  all resolved): warm runs −100% when the agent runs no tests, −69% when it
  runs them (chain breaks at pytest temp-dir numbers and `grep -r` order).
  On a 5-issue backlog in one session (33–38 calls, 4 chains): −18% — replay
  ends for the rest of the session at the first non-deterministic output.
  Safety controls passed on 22 django runs (other checkout dir, edited source
  visible or hidden from `git status`, other model: no false replay; Pass@1
  6/6 vs 8/8); an audit of all 79 replayed calls found 0 false matches.
  When it pays off: only on the same commit (an empty extra commit → 0 replay,
  because Claude Code sends recent commits with the first message), and with
  deterministic tool output (`PYTHONHASHSEED=0`: 5-issue session −18% → −65%).
  Method, limits and scripts in `bench/claude_code_replay/`.
- **A/A noise floor for the GEPA benchmark (`run_gepa_real.py --aa`).** Replays
  one recorded GEPA schedule into two plain arms to measure run-to-run noise;
  every rollout's score is now logged. On mistral-small-3.2 (3 seeds), identical
  arms differ by up to 0.07 in mean score and −4% to +8% in cost. So the small
  mistral score gains seen with ToolRecall are noise, while its 21–78% cost
  savings are well above it. Documented in `bench/results/GEPA_REAL_RESULTS.md`.
- **Real GEPA benchmark (`bench/run_gepa_real.py`, `bench/gepa_real_rig.sh`).**
  Runs the real `gepa.optimize()` loop with GEPA's LLM reflection, a
  tool-calling agent over a pinned repo revision and a real scorer, in three
  arms: GEPA default, GEPA's own `cache_evaluation`, and that plus ToolRecall
  (fork-tree tool replay + forward-proxy LLM cache). The rig runs a hermetic
  daemon, proxy and edge, with HOME pinned so the production usage CSV is
  untouched. Default `--design schedule` records one GEPA run's evaluate()
  schedule and replays it in a baseline and a ToolRecall arm, so both do equal
  work. Results in `bench/results/GEPA_REAL_RESULTS.md`:
  - Agent LLM cost −45…−78% on deepseek-v4-flash and −21…−72% on
    mistral-small-3.2 (6 runs); 25–59% of tool calls replayed; rollout scores
    equal or higher.
  - The saving tracks GEPA re-running identical parent rollouts: its cache is
    written for traced parent evaluations but never read there.
  - The first design (three independent optimizer runs) showed +1% on mistral
    because the arms evolved different prompts and did different work. It is
    kept as `--design arms` and documented as superseded.
  The synthetic fork A/B was repeated 5× on two models: 82–83%, with and
  without provider prefix caching.
- **Configurable bypass for sampled requests (`[forward_proxy] bypass_sampled`).**
  Opt-in policy for best-of-N and self-consistency workflows where requests with
  explicit `temperature > 0` require fresh random draws. Transparent caching
  remains the default; bypass can be enabled globally via `bypass_sampled = true`
  (or `TOOLRECALL_FORWARD_BYPASS_SAMPLED=1`), or per-request via
  `X-ToolRecall-Bypass-Sampled: 1`. When active, `X-ToolRecall-Cache-Sampled: 1`
  allows re-enabling cache for exact replay. Documented in `docs/FORWARD_PROXY.md`
  and `docs/CONFIG_REFERENCE.md`. Tests: `tests/test_proxy_sampling_no_cache.py` (16).

### Changed
- **Claude Code positioning corrected to measured results.** Interactive
  sessions: no token saving available (Claude Code already stubs unchanged
  re-reads; 76 local transcripts had 0 redundant reads or outputs). The proxy's
  Claude Code verdict changes from "verified, saves real cost" to "headless
  repeats only, with the `claude-code` profile". The Appendix cost model is now
  scoped to stateless agents.

### Documentation
- **GEPA / fork-tree docs caught up with what shipped in 0.8.21.**
  `toolrecall/adapters/README.md` gains a GEPA section (usage, pure-tool
  contract, `adapter.propose_new_texts = None` to use GEPA's reflection LM).
  `docs/TREE_SEARCH.md` no longer says "no billed A/B exists". Its status
  banner links the first fork A/B with its limits and notes that the naive arm
  already had provider prefix caching. It also lists the shipped tree and
  scoped-mode primitives. `docs/REPLAY_MODE.md` and `docs/CLI.md` document
  `replay tree|fork|at` and scoped modes. `tests/run_gepa_e2e_standalone.py`
  imports this checkout instead of a stale `/tmp` worktree.

### Fixed
- **LangChain tool cache keys by input and actually serves hits.**
  `ToolRecallCallbackHandler.on_tool_end` stored results under
  `{"input": <tool output>}`, and nothing ever looked a tool result up before
  the tool ran (a LangChain callback cannot skip execution anyway). So the
  documented "same tool + same args → cached result" never saved a single
  tool call. New `cached_tool(tool, ttl=None)` in
  `toolrecall/adapters/langchain.py` returns a wrapped copy of a `BaseTool`
  whose `_run`/`_arun` checks the cache keyed by tool name + parsed input
  (`{"input": s}` or `{"kwargs": {...}}`; injected `run_manager`/`RunnableConfig`
  args excluded) and stores successful JSON-serializable results. Input
  validation, callbacks and `ToolMessage` formatting still run on a hit;
  daemon down or any cache error runs the tool live. The callback handler is
  now documented as store-only and records string results under the same
  input key, so `cached_tool` can serve them. Docs (`docs/langchain.md`,
  `toolrecall/adapters/README.md`, `docs/ARCHITECTURE.md`, `README.md`, `README-pypi.md`)
  updated; they also no longer point at a `toolrecall[langchain]` extra that
  does not exist. Tests: `tests/test_langchain_tool_cache.py` (28).
- **Forward proxy: streaming responses arrived compressed and undecodable.**
  The streaming passthrough forwarded the client's `Accept-Encoding` but drops
  `Content-Encoding` on relay, so a gzip stream reached the client as raw
  bytes. Claude Code (`ANTHROPIC_BASE_URL` → proxy) logged "Stream ended
  without receiving any events" and silently re-sent every turn non-streaming,
  so each turn was billed twice while its reported `total_cost_usd` showed
  only the cheap cache-read retry. The streaming path now strips
  `Accept-Encoding` like the non-streaming path
  (`tests/test_proxy_stream_usage.py::test_client_accept_encoding_not_forwarded`).
- **`file_cache.hits` finally moves — per-file re-reads are countable again.**
  Rows were stored at `hits = 1` and never touched (no `UPDATE` anywhere), so
  `bench/cache_honest.py`'s whole report — "read once" vs "re-served", and its
  per-file savings estimate — saw every re-read as invisible. Each serve now
  bumps the persisted row in the same transaction that logs the hit (the DB is
  already written per hit for the access log, so this adds one UPDATE and no
  extra commit). `cache_honest.py`'s savings formula is corrected to
  `(hits - 1) × size/4`: `hits` includes the initial disk read, and counting it
  claimed the disk read itself as "saved".
- **`get_stats()` reports `hit_rate = "-"` for an empty window** (previously
  "0%") and gains `lifetime_hit_rate`. An empty window observed nothing; right
  after `reset-stats` that read "0%" next to a `lifetime_*` value of 96% —
  same-shaped numbers with opposite meanings. "-" matches what the TUI has
  rendered for an empty window all along.
- **`toolrecall reset-stats` moves statistics into lifetime totals instead of deleting them.**
  `reset_stats()` ended in `DELETE FROM cache_stats` + `DELETE FROM access_log`, so a
  reset — and every benchmark script that calls it — silently destroyed lifetime
  counters *and* the entire trend window. `toolrecall trend` aggregates `access_log`
  at query time and has no other source, so a reset blanked the report; the two silent
  2026-09-08/09 wipes are the same class of loss. Now every counter is moved into
  `cache_stats_banked` (the live window reads 0, `get_stats() lifetime_*` still carries
  everything, including the measured bridge-stub axis and the uncacheable denominator
  that the GC deliberately leaves alone), and the `access_log` window is compacted into
  the new `access_log_banked` (per day/category/path counts) before the raw rows go, so
  the daily trend reaches back past the reset. Banked and live rows are disjoint by
  construction, so nothing is counted twice, and the infra filter applies to both.
  `coverage()` reports the banked days separately from the live window. Tests:
  `tests/test_reset_stats_banks.py` (10; nine of them fail against the old
  delete-based implementation).
  **Observable contract change:** a category no longer disappears from
  `toolrecall stats` after a reset — its window counters read 0 while its
  `lifetime_*` values keep carrying the total, so the number is visible instead
  of merely stored.
- **Context-savings totals can no longer exceed IO-offload totals after a GC pass.**
  The housekeeping reset zeroes five `cache_stats` columns for idle categories, but
  `cache_stats_banked` only had four of them, so `tokens_saved_to_llm` was reset in
  place while `io_tokens_saved` was banked: the lifetime view then claimed *more*
  context savings than IO offload — impossible, since context savings are a subset
  of IO offload. Seen live on 2026-10-22 (`file_cache`: ctx 74,264,365 vs io
  65,033,600). The ctx axis is now banked and reset like the others, with an
  `ALTER TABLE` guard so bank tables from older builds gain the column instead of
  silently dropping banked ctx totals. Note: the *historical* residue on the live
  DB stays visible and is unrecoverable — the missing IO was never banked. Tests:
  `tests/test_gc_counter_wipe.py` (+2: ctx banking, legacy bank-table upgrade).
- **`make validate` is green again.** `test_shim_tagging` stripped only
  `HERMES_*` env vars, so it failed in any other agent session (`CLAUDECODE`,
  `CODEX_`, …). Its setUp now strips every agent marker the shim knows.
  `.gitleaksignore` allowlists the Langfuse fixtures' cache-key hashes, which
  gitleaks flagged as API keys after 0.8.23.

## [0.8.23] — 2026-10-21

### Added
- **Langfuse v4 framework adapter (`adapters/langfuse.py`).** Drop-in cached tool
  wrapper with verdict recording in span metadata and a multi-rung truncation
  ladder guarding against Langfuse's 9KB metadata ceiling to prevent metadata
  loss. 34 tests in `tests/test_langfuse_adapter.py`.
- **Request-dedup attribution by tool (`dedup_by_tool` + `toolrecall dedup-stats`).**
  Attributes request body deduplication savings to the tool that produced them
  (e.g., repeated `skills_list` calls). Tracks blocks, tokens saved, and shares.
  Tests in `tests/test_dedup_by_tool.py`.
- **`work_key` — Layer-0 identity for a unit of work (`keys.py`).** Answers
  *"have we already paid for this unit?"* for a candidate artifact (plus
  optional evaluation parameters) instead of for a request — the level at which
  an evaluation is worth reusing across processes, runs, and workers. Two
  deliberately separate spaces: the legacy-compatible form
  (`content_digest(code)`, optional `":" + stage`) is byte-identical to the
  evaluator memo keys that predate this function, so stores already on disk
  keep hitting — pinned by a golden vector captured from a real OpenEvolve memo
  store; the params form is versioned (`w/v1:`) over `jcs.canonicalize`
  (RFC 8785) so dict order cannot fork a key. Purely additive: no existing
  preimage changes, `tests/golden_keys.json` keeps its old vectors. 6 tests in
  `tests/test_keys.py`.
- **Work-unit ledger (`work_ledger` table + `work_store` / `work_lookup`).**
  The persistence half of `work_key`: `work_store(key, metrics, eval_time, ttl)`
  records one PAID evaluation — `attempts` advances on a re-roll while the first
  known result is never overwritten — and `work_lookup(key)` recalls it (None
  when unknown, TTL honored). Counters land in their own `work_cache` category,
  so reuse is visible in `top` and the healthcheck instead of borrowing another
  tier's label — the same visibility gap that proxy dedup still has. Daemon
  commands `work_store` / `work_lookup`, thin client wrappers with a
  direct-SQLite fallback, malformed requests fail closed. Unit tests plus a
  socket E2E test (`tests/test_work_ledger.py`,
  `tests/test_e2e_work_ledger.py`).
- **OpenEvolve evaluator memo (`adapters/openevolve.py`).** Wraps an eval
  script's `evaluate` / `evaluate_stageN` so a repeat of an already-paid
  candidate is served from the work ledger. Opt-in via
  `TOOLRECALL_OPENEVOLVE_MEMO=1` (pure pass-through without it); `reuse_after(K)`
  makes the reuse boundary explicit so deliberate re-rolls stay paid;
  `impure=True` refuses to memoize an evaluator that is not a pure function of
  the candidate; hits count in the `work_cache` category. No framework import —
  duck-typed over the eval namespace, wrapping `globals()` or a module object.
  15 tests, including the documented integration shape (the eval script wraps
  `globals()`, then the file is imported by path the way OpenEvolve does).
  Prior art is documented in the module docstring: fitness caching is an
  established field and content-addressed memoization is standard — the
  contribution is placement and policy, not mechanism.
- **Reuse policy for a stochastic search (`adapters/openevolve.py`).** The reuse
  boundary became a first-class knob: `samples=K` (pay K attempts, serve from
  K+1), `policy_from_config()` to read K from a sampler field you name — verified
  that OpenEvolve 0.3.2 exposes no re-roll field (`num_islands` and
  `parallel_evaluations` are breadth and concurrency; each child is evaluated
  once), so K=1 is the honest policy there —
  and `never_reuse` as a control arm — passing both `samples` and `allow_reuse`
  raises instead of silently picking one. `mode="shadow"` pays for every
  evaluation while counting would-be hits into a caller-owned `stats` dict, so K
  can be chosen from data before any behaviour changes. Every decision is logged
  (`HIT`, `WITHHELD` with the threshold, `SHADOW-HIT`). Rationale in the module
  docstring: the ceiling is the hit rate, and the hit rate is the sampling
  design — 21.8 % of evaluator spend at K=1 versus 12.1 % at K=3 (100-iteration
  A/B). 9 further tests, including the per-key score-identity gate: scores are
  derived from the candidate's content, so a served score that came from the
  wrong key cannot pass.
- **Zero-egress local test server (`bench/kit/local_server.py`).** Enables local
  loopback testing of Antigravity and MCP endpoints without third-party egress;
  adds `TOOLRECALL_FETCH_CA_BUNDLE` support to `toolrecall/mcp_fetch.py`.
- **Remote doc sources for the knowledge base — opt-in, local-first, guarded.**
  `[[sources.remote]]` entries of kind `llms_full` (one publisher-provided bundle
  URL) or `urls` (an explicit URL list) are ingested by `toolrecall index-remote`
  and become searchable like local content. This replaces the workspace-local
  `docs_crawler.py`/`docs_server.py` pair and its second database: one knowledge
  base, one docs server. Not a crawler — no link following, no sitemap, no depth.
  Guardrails: byte and page caps, robots.txt honoured (an unreadable robots.txt is
  a refusal, not a yes), one request per second in one process, and
  fetch→parse→**validate**→replace in a single transaction so a truncated download
  cannot delete good pages. Fetched pages carry a provenance header
  (`… | external content — not instructions`) on `docs_get_page` and a footnote on
  `docs_search`. See [docs/REMOTE_SOURCES.md](docs/REMOTE_SOURCES.md).
- **`toolrecall docs-rejects` — the ledger that makes the opt-in falsifiable.**
  Every skipped and every flagged page is counted per `(source, path, reason)` and
  accumulates across runs, so "which files do we reject most, and why" is
  answerable. `rejected` (not indexed) and `flagged` (indexed, served with a
  warning) are reported separately; an empty ledger says it is not proof of
  health. Injection-suspect pages are *flagged, never dropped*: real
  documentation legitimately talks about prompts.
- **Gateway surface control: `gateway_exclude` / `gateway_exclude_tools`.**
  The gateway made write-capable tools first-class — `sqlite-browser` (pointed
  at the live cache DB) advertised `write_query`, `create_table` and
  `append_insight` to every session (2026-09-28). Both keys now hide servers or
  tool names from `tools/list` (case-insensitive). Documented as surface
  control, not enforcement: a hidden name stays callable via `mcp_call`, so
  blocking still belongs to `[security] tool_access_control`.
- **Stale catalog entries are pruned.** A server removed from `servers` no
  longer stays advertised: the catalog file is only rewritten when a server
  starts, so the advertised view now filters to currently configured servers.
- **MCP replays are visible in the daily series.** `mcp_cache` records write a
  synthetic `mcp:<server>/<tool>` path into `access_log`, and
  `trend.daily_trend(days, category)` is per-category, so the TUI trend view
  shows an `MCP server replays` section. Kept separate from the file_cache
  series on purpose: an MCP replay is I/O saving only (the body still reaches
  the model), so summing the two would overstate cache value.
- **Session view: unchanged stubs, diffs and line ranges — one state machine.**
  The bridge's session state now lives entirely in `stub_core` (the shim keeps
  the stub-only `maybe_stub`; the bridge uses `present`, which adds a unified
  diff when a file changed, and `read_file` gained `offset`/`limit`). Freshness
  is the TTL *plus* a session budget (tool calls and emitted tokens since the
  full copy was sent — `stub_core.DEFAULT_MAX_CALLS` / `DEFAULT_MAX_TOKENS`,
  derived from the same 396-run measurement as the 240s TTL) *plus* the
  consecutive-suppression armor; terminal stubs now expire on that budget
  instead of never. A diff is always computed against the agent's last FULL
  copy, never against a diff we previously sent: the bridge cannot know whether
  a harness applied one, and a wrong assumption would compound silently.
- **Compaction signal.** `context_compacted` (MCP tool, or
  `toolrecall context compacted` from a PreCompact hook) bumps a daemon-wide
  epoch; bridges pick it up on their next read and re-send full content. An
  exact signal beats guessing at the compaction horizon.

### Fixed
- **`cached_patch` false `already_applied` and lossy patch/write byte translation.**
  `cached_patch` checked substring containment which falsely reported deletions
  (`new_string == ""`) and earlier occurrences of `new_string` as already applied,
  silently skipping real patches. Write/patch now compare on-disk bytes to preserve
  CRLF line endings and non-UTF-8 bytes without corrupting them.
- **Cache layer caller-relative path resolution (`resolve_caller_path`).**
  Previously relative paths were resolved against daemon CWD instead of caller CWD,
  causing path key collisions across projects. Paths are now made absolute at the
  client and MCP bridge boundary.
- **Knowledge DB worker thread-local path pinning.**
  `_maybe_refresh_index_async` spawned a worker that resolved DB path after caller
  environment was gone, intermittently hitting production DB during test teardown.
  Worker path is now pinned in thread-local storage at spawn time.
- **Garbage collection counter banking (`cache_stats_banked`).**
  `garbage_collect()` zeroed stats for categories idle for 24h, destroying lifetime
  IO token counts and inverting the context-to-IO savings ratio. Counters are now
  banked to `cache_stats_banked` and exposed via lifetime metrics in `toolrecall status`.
- **`uv.lock` re-tracked, anyio bumped 4.14.1 → 4.15.1.** Clears Dependabot security
  alerts GHSA-82r6-8w77-94w6 and GHSA-5p39-cfhj-2xmp.
- **`toolrecall proxy attach` was a silent no-op on a config without a `base_url` line.**
  It only rewrote an *existing* `llm.base_url:`; a config whose LLM section carried
  none — the state a partial revert or a hand edit leaves behind — made attach print
  "no llm.base_url found in Hermes config — skipping" and leave the agent unproxied,
  with the proxy CSV as the only evidence. Attach now **inserts** the line as the
  section's first key, reusing that section's own child indentation, so the agent ends
  up proxied either way; a config with no `model:`/`llm:` section is still refused and
  names the manual `OPENAI_BASE_URL` route instead. Regression coverage in
  `tests/test_proxy_attach_config.py` (attach previously had no tests at all).
- **`test_ast_performance` is no longer load-flaky.** The single 1000-call mean
  measured 1.18ms inside a full-suite run while the same check costs 0.08ms
  standalone (2026-09-28, 14x swing from the suite's own interpreter state) and
  failed unrelated merges. It now measures best-of-5 batches with GC disabled —
  a real regression raises every batch.
- **`mcp_call` `bypass_cache` was ignored.** The bridge sends `ttl=0` for a
  bypass; the daemon read only the server config, so a caller explicitly asking
  for a fresh read was served the cached body and had no way to tell. A
  per-request `ttl` now wins over the server TTL.
- **Side-effecting MCP tools were cached.** No read/write classification
  existed, so a repeated `create_issue` inside the TTL returned the stored reply
  and *never reached the server*. Tools are now classified by the server's
  `readOnlyHint`/`destructiveHint` annotations, else by read/write verbs in the
  name (`cache.mcp_tool_is_cacheable`); a write is never served from nor stored
  in the cache, and its call invalidates that server's cached reads. The
  residual risk — an unannotated mutating tool with an opaque name — is
  documented on the classifier and self-heals through that invalidation.
- **`browser_cache` rows never expired and were never purged.**
  `BROWSER_CACHE_TTL` existed but nothing read it, so a cached page was served
  for the lifetime of the DB (a silent stale delivery) and the table only ever
  grew. The check now expires rows and the GC reclaims them.
- **`browser_cache` and `terminal_cache` hits recorded `io_tokens_saved=0`.**
  Both computed the token estimate and then dropped it, so the TUI IO-offload
  column read 0 despite real replays — the same class as the api_cache
  (2026-09-21) and mcp_cache (2026-09-24) fixes.
- **`toolrecall setup` crashed on a non-tty stdin.** `cmd_setup` decided
  "interactive" from `--yes` / `TOOLRECALL_NONINTERACTIVE` alone, so a scripted
  or CI run printed the API-proxy question and then died with `OSError:
  reading from stdin while output is captured` — the prompt sites caught
  `EOFError`, but a non-tty stdin raises `OSError`. The flag now also requires
  a tty, and every prompt site treats an unreadable stdin as "no answer"
  (its documented default) instead of crashing. Same class fixed in
  `toolrecall cache reset` and the Claude Code install prompt.
- **`test_shim_installed_pkg` failed instead of skipping in a bare checkout.**
  Its documented skip contract ("skipped if toolrecall is not pip-installed")
  was enforced only on the system-python branch, so a venv without
  `pip install -e .` failed with `ModuleNotFoundError` — a false gate failure
  on exactly the environment the docstring names. The probe now runs
  unconditionally AND from a neutral cwd, since the repo root shadows the
  installed package (the very blindspot the test exists to catch).
- **A config typo could silently discard the ENTIRE config.** The unknown-key
  checker runs inside the try/except that falls back to package defaults, and
  under `warnings.simplefilter("error")` (`-W error`, some CI) a plain
  `warn()` raises — so an unknown key escalated into `CONFIG IGNORED`,
  dropping `[mcp] allowed_paths` and `[network] tokens` and leaving the
  instance on package defaults. Those diagnostics are now advisory: they still
  print, but can never abort the load.
- **`[[sources.knowledge]]` was reported as an unknown key.** The table is read
  by `docs.py` and documented in `docs/KNOWLEDGE_DB.md`, but `knowledge` was
  missing from the `[sources]` key list, so following the docs produced a
  false "unknown key (key ignored)" warning — the same false-positive class
  the file's own comments record fixing for `[network.clients]`.

### Changed
- **Savings counters split into two unmistakable names (`cache_stats`).**
  `cache_stats.tokens_saved` had drifted into meaning "IO offload" while
  reading like "tokens saved to the LLM" — the two are different claims
  (the first is disk/subprocess work avoided, the second is tokens that
  never entered the model's context), and a consumer quoting the wrong one
  overstates savings. Columns are now `io_tokens_saved` and
  `tokens_saved_to_llm`; the stats JSON emits the same canonical keys
  (`io_tokens_saved`, `tokens_saved_to_llm`) alongside the long-standing
  measured aliases (`tokens_served_from_cache`, `tokens_not_read_from_disk`,
  `tokens_not_sent_to_llm`), and the ambiguous `context_tokens_saved` key is
  gone. Migration is an in-place SQLite `RENAME COLUMN` (idempotent, values
  preserved; applied on first open of an older DB). TUI labels follow:
  the IO table column reads `IO saved`, the headline `SAVED TO LLM TODAY`
  (never sent to the model).

### Fixed
- **Shim drift detection is content-based, not version-based (`venvs.py`).**
  `ensure_shim` compared `importlib.metadata` versions, which passes whenever
  the metadata agrees while the code differs — a hand-synced snapshot, an
  editable install whose dist-info lags its tree, or a checkout running ahead
  of an unchanged `__version__`. In that state `pip install --upgrade
  toolrecall` answers "requirement already satisfied", `ensure_shim` returned
  True, and `toolrecall shim --status` printed ✅ for a venv whose shim modules
  were dozens of commits behind — silently keeping pre-fix shim behaviour
  forever (found live: a Hermes-venv copy 60 commits behind mis-tagged reads
  under the agent's TMPDIR, because the relative library-marker fix never
  reached it). The shim modules (`shim.py`, `shim_stub.py`, `stub_core.py`,
  `client.py`, `keys.py`, `config.py`) are now fingerprinted by content on both
  sides (`_running_manifest` / `_venv_manifest`: one `content_digest` over
  sorted name+bytes, missing files marked) and drift triggers the existing
  index upgrade plus — when the running install is a source tree — a repo
  overlay (`zz_toolrecall_repo.pth`, same mechanism as
  `scripts/repo_overlay.py`, stale snapshot dir removed first). `ensure_shim`
  now returns False when the fingerprint still differs instead of reporting a
  false "verified"; `shim --status` prints the venv-vs-install version pair and
  a `DRIFT` line with the fix command. 7 new tests in `tests/test_venvs.py`.

- **Billed-truth hygiene: two test harnesses were writing fake rows into the
  production usage log** (`tests/test_proxy_dedup.py`,
  `tests/test_proxy_resilience.py`, `tests/conftest.py`).
  `ForwardProxyHandler` appends one row per request to `_USAGE_LOG_PATH` — by
  default `~/.toolrecall/proxy_usage.csv`, the billed-truth file every savings
  figure is computed from. Two harnesses drive that real handler and never
  pinned the path, so every run appended fake rows: **1106** from
  `api.deduptest.invalid` and **942** from `api.testretry.invalid:80` (2057
  fake-host rows total, ~1.9M bogus `dedup_tokens` — i.e. the file's dedup
  totals were ~93% test junk). Both now pin the path to a per-class temp file
  and restore it in `tearDownClass`, matching `tests/test_usage_dedup_column.py`.
  Root fix in the same change: the autouse production-path guard wrapped
  `sqlite3.connect` and `socket.connect` but **not file writes**, so a plain
  `open(path, "a")` into `~/.toolrecall` was invisible to it — it now also wraps
  `builtins.open` and records a violation on any write into production state
  (reads stay unflagged), turning this whole leak class from silent to loud.
  Writes that code-under-test legitimately makes — live log appends (the
  logging rail is default-ON) and the atomic update-hint cache — are carved out
  by path (`_PROD_WRITE_EXEMPT`); the billed-truth usage log is not, which is
  the point. Verified: the two harnesses run (21 tests pass) with the fake-row
  count in the production CSV unchanged (2057 → 2057), a probe writing into the
  usage log fails the guard, and the four pre-existing tests that tripped the
  new arm all pass. The pre-existing junk rows are **not**
  removed — that is production data and a separate decision.

- **Guard coverage extended to the live *config* home** (`tests/conftest.py`,
  `tests/README.md`). `_PROD_PATTERNS` covered `~/.toolrecall`, the daemon
  socket dir and `:8569` — but not `~/.config/toolrecall`, so a test writing
  `toolrecall.toml`/`.env` there was invisible to the guard: the same
  blind-spot shape as the usage-log leak, one directory over. Writes there now
  fail the test; reads stay allowed (many tests legitimately load the live
  config). Verified: a probe write to `~/.config/toolrecall/toolrecall.toml`
  fails the guard while a read passes and the file's sha256 is unchanged, and
  `make validate` stays green — i.e. nothing in the suite writes there today.
  An earlier one-off warning naming the live config with an unknown `[x]`
  section did **not** reproduce in a full run and left no trace; most likely a
  concurrent external writer, not the suite.

## [Unreleased] — 2026-09-24

### Added
- **Request-body dedup now covers Anthropic-shaped `/v1/messages` bodies.**
  The forward proxy's opt-in dedup (`[forward_proxy] dedup`) previously applied
  only to `/v1/chat/completions`. The transport-neutral core now walks
  messages-style blocks structurally and recurses into `tool_result` content
  (where an agent loop's repeated payload actually lives), plus messages-style
  `image` blocks behind `TOOLRECALL_DEDUP_MEDIA`. Blocks are shallow-copied
  and only the text leaf is replaced, so `cache_control` / `tool_use_id`
  survive byte-exact. Prefix stability across turns holds only with
  `protect_last=0`; the shipped default (`2`) defers a duplicate that arrives
  inside the protected tail by one turn, and stubbing it then rewrites
  already-cached bytes — see `toolrecall/dedup.py` and
  `tests/test_dedup_anthropic.py::TestProtectedTailVsCache`. New
  path gate `_is_dedup_path`; new suite `tests/test_dedup_anthropic.py`
  (Anthropic shape, cross-turn prefix stability, marker preservation,
  `/v1/messages` proxy round trip). Mechanism-tested on this path; the
  chat-completions path remains the only billing-verified one (−32.3% prompt
  tokens / −30.0% billed).
- **`docs` built-in MCP server (`toolrecall/mcp_docs.py`).** Exposes the
  in-core FTS5 knowledge base (`docs_search`, `docs_get_page`) as a
  standalone multiplexer server — the same tools the `toolrecall mcp`
  bridge offers natively, now auto-resolvable via the registry. Zero
  runtime dependencies (stdlib + SQLite FTS5).

### Changed
- **Default multiplexer server set:** `["time", "sequential-thinking",
  "cache-fs"]` → `["docs", "fetch", "cache-fs"]` in the shipped
  `config.toml` template. `fetch_revalidate = true` enabled by default.
  `time` remains registered and opt-in; `github` remains opt-in
  (write access). Docs examples updated accordingly.

### Fixed
- **TMPDIR-nested agent-home hosts: 6 pre-existing test failures + 3 source
  defects.** On hosts where the tempdir lives under the agent home (Hermes
  scratch: `TMPDIR=~/.hermes/cache/scratch`), the shim's library-marker gate
  (`/.hermes/`), the tracker's default ignore prefixes (`~/.hermes/cache/`),
  and literal `/tmp` assumptions in tests collided with real agent-home
  paths. Source fixes: (1) shim `_looks_like_library_path` matches markers
  against the path RELATIVE to `gettempdir()` — the tempdir's own path no
  longer satisfies position-independent markers, while simulated venv
  structure inside tmp still classifies "shim"; (2) context-tracker literal
  ignore prefixes are now anchored at a directory boundary for every entry
  (the old `p.endswith(os.sep)` ternary tested the pre-`abspath` value and
  was always False for slash-terminated prefixes — i.e. every config
  default — so `"/tmp/sessions/"` degraded to unanchored `"/tmp/sessions"`
  and also matched `"/tmp/sessions_extra"`; root `"/"` kept intact);
  (3) single-value list-typed env overrides (`TOOLRECALL_TRACKER_IGNORE_PREFIXES`,
  `_SHIM_EXCLUDE_PREFIXES`, `SKILL_DIRS`, `SCAN_DIRS`, `MULTIPLEX_SERVERS`)
  were stored as a bare string and silently dropped by the properties'
  `isinstance(raw, list)` checks — `_apply_env_overrides` now has
  `_LIST_KEYS` (comma-split, single value wraps as 1-element list), and
  `tracker_ignore_read_prefixes` degrades a str value to a 1-element list
  instead of resetting to defaults. Test fixes: tracker unit test derives
  the ignored prefix from the actual `mkdtemp` location (literal `/tmp`
  never matched on scratch hosts) and pins the anchoring contract with a
  sibling-dir partner; pid-gate and shim tests verified passing under
  `TMPDIR=/tmp` (environmental, no product change needed). Silent-swallow
  baseline +1 config.py (tempdir carve-out is fail-soft), +1 shim.py
  (relative-marker guard on the open() hot path). Full unit suite green:
  1657 passed.

## [Unreleased] — 2026-09-20

### Changed
- **MCP bridge stub TTL 60s → 240s (measured).** The TTL guards the
  AGENT-memory axis — "does the agent still hold the earlier content?" —
  which mtime+size cannot answer (that proves only the DISK content is
  current). It must therefore track the real compaction horizon, and 60s
  did not. Measured over 396 conversation-growth runs in the billed usage
  CSV (2 months, monotone prompt growth until reset): median 14 turns /
  4.8 min before a context reset (p25 1.9 min, p75 19.1 min), median 18.5s
  per request — so 60s covered only ~3.2 requests of a live conversation
  and expired stubs whose content the agent provably still held. 240s sits
  just under the median horizon (288s) and well below p75; `max_consecutive`
  stays the hard armor. The shim already used 300s — only the bridge moved.
  Explicitly NOT done: moving stub state into the daemon. Cross-process
  state would make the stub's claim ("you already hold this") false for a
  process that never read the file — the same class of failure that
  corrupted a gateway YAML parse on 2026-09-18 and motivated the
  `agent_tool` gate.
- **Saved view: "not sent to the model" is the headline.** That is where
  the saving is, so it leads. Below it: sent vs without-ToolRecall with the
  multiple ("1.57x more for the model to read"), and the not-sent figure
  translated into both a multiple of what was sent and whole context
  windows. The bill passage and the today-footer were removed from the
  user view (the billed numbers stay in the Details line).
- **The context limit is MEASURED, not assumed.** The hard-coded 200K was
  empirically wrong: a 245,947-token request was accepted and billed, so
  the view counted real, accepted requests as "over the limit" (130 naive
  vs 130 actual — a null statement). The limit is now
  `max(floor, largest actually accepted request)`, exposed as
  `replay["limit"]`, and the limit line renders only when the naive world
  actually crosses it. Same for the biggest-message line: shown only when
  `peak_naive > peak_actual`, with the explicit note that it is the same
  removed content piled up again, not an extra saving.

### Added
- **Session-replay tier in `toolrecall top` — the naive-world equivalent, measured not guessed.**
  The umbrella counts removed tokens once per event; it structurally cannot see the
  amplification that produces 7-9x session length (removed content rides along in EVERY
  subsequent request of a naive conversation). New `session_replay_today()` reconstructs
  the counterfactual deterministically from today's wire data: sessions split at >15 min
  request gaps, each naive request = billed prompt + in-request dedup + all stub tokens
  stubbed earlier in the session (HIT rows excluded; stubs attach via the same gap rule;
  `tui_top._CONTEXT_WALL` = 200K marks requests the naive world cannot make). Every input
  is measured (billed prompts, wire-verified stubs, logged timestamps) — the construction
  is the replay, a disclosed segmentation choice, never a hidden multiplier. Rendered as
  the REPLAY tier in view 1 + non-tty (`session replay (naive-world, today): X vs actual
  Y — Z% saved (amp xN, S sessions)` + wall line). First prod day: 230.2M naive vs 61.4M
  actual = 73.3% saved, amp x3.8, 297 naive wall-crossings vs 47 actual — the number that
  reconciles a 2% umbrella with 7-9x session length. Tests:
  `tests/test_session_replay.py` (replay arithmetic across 2 sessions with a dropped
  stub, HIT exclusion, wall counts with a tunable constant, no-traffic None, render
  contract via snapshot key).

### Changed
- **Saved view rewritten in plain words (2026-09-20): the headline is sent + avoided.**
  User bar: "76M was sent and 168M was avoided, that's the headline — explain it
  without stub/dedup/naive jargon; the 200K/304-vs-54 line needs a coherent
  sentence." New TODAY-line (`sent + avoided = without ToolRecall`, always
  reconciling — without replay data the floor is the counted-once removals),
  a plain-words list (removed parts answered from memory / removed once = not
  re-sent, with the measured `ride_slots` count instead of a request-1
  approximation; the ride-along sentence only appears when later requests
  actually carried removed content), the context-limit line with its
  definition ("the largest context the model accepts at all"), the dollars
  sentence ("avoided tokens never reached the provider — nothing on the bill
  paid for them"), and one compact Details line. No jargon in the user view
  (stub/dedup/naive/replay/amp/wall live in HELP_TEXT and the code only).
  All labels English. Tests updated to the plain-word strings.
  User finding (2026-09-20): "68.8% less context but only 1.6% held back —
  how do these relate?" Answer rendered in `top` itself: section 1's header
  says "(each removal counted once)", section 2 leads with an add-up line —
  `actual X + avoided Y = naive Z (p% less context load)` — and a breakdown
  (`avoided = D deduped blocks (once) + S stubbed content riding along in N
  later requests (xM)`). The replay collector exposes the new fields
  (avoided_total, dedup_sum, stub_attached_tokens, amp_stubs); naive =
  actual + avoided is an identity of the construction, so the line always
  reconciles exactly. HELP_TEXT documents the bridge (1 counts once at the
  moment of removal; the naive world pays it again in every later request —
  the difference IS the session effect).
- **`toolrecall top` saved view: three sections, one question each (2026-09-19).**
  The view had grown to ~10 numbers answering three different questions —
  re-arranged as: `1 HELD BACK (measured)` (today's wire-held-back tokens =
  stubbed re-reads + deduped blocks, once per event), `2 SESSION CAPACITY
  (replay)` (naive-world equivalent for the same sessions, incl. the 200K
  wall line), `3 BILLED (provider)` (billed tokens + cost + provider
  prefix-cache line). Demoted OUT of the saved view: the cumulative stub
  ledger ("logged days" line — available via `toolrecall stats`), the
  median-request diagnostic (View 4 keeps it), the replays parenthetical
  (folded into section 3). All labels in English. HELP_TEXT now documents
  the three questions and the mixing rule (small 1 + large 2 is expected —
  amplification is the session-length effect). No collector logic changed —
  re-layout only; tests updated to the new strings.
- **`toolrecall status`: the estimate label is now `context_estimate=`.** The
  per-cache context column was printed as `tokens_not_sent_to_llm=` — the
  same name the MEASURED tier uses (`tokens_not_sent_measured`), so the
  estimate (per agent-tool-HIT counter) read as billed-truth. Human label
  renamed; the underlying stats JSON keys are unchanged (script/cron
  consumers unaffected).

### Added
- **Provider prefix-cache visibility (CSV v4).** Post-mortem 2026-09-19: all 44k
  `proxy_usage.csv` rows logged `cache_read_tokens=0` although the discount was
  flowing — OpenRouter reports cache reads NESTED in
  `usage.prompt_tokens_details.cached_tokens` (+ `usage.cache_discount` in
  dollars), not in the flat OpenAI/Anthropic fields the v3 parser read. The
  CSV grows a 12th column `cache_discount_usd` (header migrates in place;
  pre-v4 rows read 0 via DictReader `.get() or 0`, same contract as
  cost_usd/dedup_tokens); the writer now fills `cache_read_tokens` from the
  nesting (flat fields still win). `toolrecall top` view 1 + non-tty render
  gain a bill-side line — `provider prefix-cache: X tok re-served by provider — p% of sent [· discount $Y (provider-reported)]` — shown only when the
  provider reports cache activity, and deliberately OUTSIDE the not-sent
  umbrella (the discount discounts SENT tokens; umbrella = never-sent).
  Dollars appear only when the provider reports them (0 = not reported,
  never guessed). Tests: `tests/test_proxy_cache_usage.py` (nested parse,
  flat precedence, zero state, pre-v4 row back-compat, render contract);
  `tests/test_proxy_stream_usage.py` assertions updated to the v4 contract
  (nested reads now land in the column — the old "nested is ignored" pin).

## [Unreleased] — 2026-09-18

### Fixed
- **Daemon IPC `cached_mcp_store` now addresses the row `cached_mcp_check` reads (key-alignment bug) + `[cache].hash_algorithm` default is `sha256` (was `md5`).** Found by the 2026-09-19 daemon-in-the-loop replay bench (400-call sim↔daemon parity run): the store handler wrote the `mcp_cache` row under the raw IPC key while the check handler looked up the wrapped derivation `_mcp_request_key("_generic","_lookup",{"key": K})` — every IPC-stored row was unreachable by checks (zero hits at 0.0 s repeat gap; bench-DB row hash == raw key, check-derived ≠ raw). Fix: store derives its row key through the same single derivation cache.py owns (never re-derive at call sites). Hash default flip per identity-portability: daemon cache keys now match external sha256 derivations (SEP `mcp.tool.request_key` family); `md5` remains selectable as legacy opt-out (`TOOLRECALL_HASH_ALGORITHM=md5`, zero-rewarm rollback). One-time cost: existing rows miss once and re-warm; mtime invalidation unaffected; billed-truth proxy path (`api_request_hash`) decoupled. Tests: `tests/test_mcp_ipc_key_fix.py` (5 — default/opt-out pins via subprocess, unit row alignment, two E2E store→check round-trips through a real daemon). Pre-existing at base, unrelated: `test_canonical_e2e` (3), `test_swr_e2e` (12) — "no such table" ordering, outside validate's selection.

### Changed
- **`uv.lock` re-tracked, anyio bumped 4.14.1 → 4.15.1.** The lockfile was removed
  from tracking in `83e96ce` (June) but Dependabot kept alerting on the dead
  manifest: GHSA-82r6-8w77-94w6 (critical, TLSStream IDNA host encoding → TLS
  certificate spoofing) and GHSA-5p39-cfhj-2xmp (medium, process-pool stderr
  deadlock) — both patched in anyio ≥ 4.14.2. The lock is re-tracked resolved at
  4.15.1 so both alerts close when the stack pushes; lockfile-only change, anyio
  stays transitive (httpx, starlette, mcp, langsmith, sse-starlette).
- **`toolrecall top` view 1: the not-sent number is one umbrella total with a per-mechanism breakdown — and every number is day-scoped honestly.** Two different counters both read as "not sent today": `not sent (dedup)` (proxy usage CSV — byte-identical blocks removed within a request) and `tokens never sent to the LLM` (cumulative `cache_stats.tokens_not_sent_measured` — bridge stub events), the latter printed under a "today" header despite spanning all logged days (12.6M cumulative next to 15.9M billed-today read as if ~11M had been saved today). Now view 1 (and the non-tty `render_plain`) lead with `sent to LLM today` vs `not sent (measured): X tok — p% of would-have-been-billed`, where X = stubbed re-reads (today, summed from `access_log` category `mcp_dedup` via new `stubbed_today(db)`; read-only, `_open_ro`, day boundary UTC midnight matching `money_today()`) + deduped blocks (proxy CSV `dedup_tokens`) — disjoint, wire-verified sets (stubbed content never reaches the proxy, so the proxy can only dedup what was actually sent), with each mechanism on its own line; the cumulative counter appears only as `stubbed re-reads, logged days: Y (today: X)` — "logged days" because `access_log` is a rolling ~50k window, not since-forever history. Percentage base fixed: `removed / (billed + removed)` — the provider never billed the removed tokens, so "% of billed" was the wrong denominator. Zero-state names its reasons ("no stubbed re-reads, no duplicate blocks today"). HELP_TEXT CONTEXT tier states the two mechanisms and their disjointness. Tests: `tests/test_tui_top.py` (+4: day/window split across UTC midnight, empty-db `(0, 0)`, umbrella headline math (57K = 50K dedup + 7K stubs; 36.3% = 57/157 pre-removal base) with the old mislabeled forms asserted gone, zero-state reason line; render tests monkeypatch `money_today` — the prod `proxy_usage.csv` must never be read there). Docs: `docs/CLI.md` view list synced to the shipped view order.

## [0.8.22] — 2026-09-15

### Added
- **`stats` carries a context_flow verdict + `[hermetic]` log tag.** `toolrecall stats` embeds the live tracker snapshot (armed/checkpoint state, banked totals, dirty/clean counts) as a `context_flow` block in its JSON — the 2026-09-15 cron reported an armed flow with 593k banked tokens as "inactive" because that state was invisible in the stats JSON; fail-soft when the daemon is down (explicit reason field, never a bare counter). Test daemons/pytest now tag every log line `[hermetic]` (`TOOLRECALL_HERMETIC=1`) so shared log files never let test-suite lines masquerade as production traffic ("tracker snapshot saved: 0 read paths" was twice misread as a dead production flow). Tests: `tests/test_cli.py` (context_flow block, fail-soft), `tests/test_logging_setup.py` (+2: tag present when env set, absent when unset), `tests/conftest.py` sets the env hermetically.

### Changed
- **F2 completion: the auto-updater no longer installs silently — `[update] auto_apply`, default `false`, with an agent-facing notice channel.** The 2026-09 review's top finding: with install-on-detect, a compromised PyPI account meant silent code execution on every user's next CLI run. The CLI hook now only *announces* (`⬆️  X available — run toolrecall update`) and records the version; installing is an explicit `toolrecall update` (or `[update] auto_apply = true` / `TOOLRECALL_UPDATE_AUTO_APPLY=1` to restore the old behavior, index pinning still applies). Because agent-driven users never run the CLI — and a stdout notice dies with the command — the daemon serves `update_pending_hint` (updater state: `latest_seen`, never a request-path network call) and the MCP bridge appends it to the **first tool result of each session**: `[toolrecall] Update available: 0.8.21 → 0.8.22 (run: toolrecall update)`. Throttled once per version (`notified_for`) client- and daemon-side; a daemon-side hourly refresher thread keeps `latest_seen` current for setups that never touch the CLI (first pass 60s after boot, interval-gated by `check_interval_hours`). Escapes: `[update] enabled = false` kills the check entirely; multiplexer-only bridges stay quiet by design. Tests: `tests/test_update_hint.py` (6: hint content, once-per-version, re-notify on new version), `tests/test_update_hint_channel.py` (9: daemon handler + throttle + never-raises, bridge first-call emission, exactly-once per session, daemon-error fail-soft), `tests/test_updater.py` (+2: notify-only default pins `apply_update` NOT called, opt-in apply path). README, README-pypi, shipped `config.toml`, `toml_serializer.DEFAULT_CONFIG` synced.
- **F1: the lexical symlink rescue is now OPT-IN (`[security] allow_symlink_escapes`, default `false`).** A stock install enforces strict realpath-only containment (Layer A) — no symlink inside an allowed directory can widen what is readable. Rationale (2026-09 review): the Layer-B rescue is the easiest attack surface on Windows (junction reparse points published by any writer inside an allowed dir), and the `/etc/os-release → /usr/lib/os-release` justification is POSIX-only. Layouts that legitimately need the rescue opt in with `allow_symlink_escapes = true` / `TOOLRECALL_ALLOW_SYMLINK_ESCAPES=1`. Flipped at every fallback surface (Config property, daemon/client `getattr` fallbacks, `path_utils.check_path_allowed` signature, `toml_serializer.DEFAULT_CONFIG`, shipped `config.toml` template) — pinned by `tests/test_symlink_default.py` (8). SECURITY.md, README, README-pypi synced.
- **Malformed config files now warn loudly instead of degrading quietly.** `Config._load` could not load a file → single generic warning → every setting in it silently reverted to package defaults — fail-open for a file carrying `[mcp] allowed_paths` and `[network]` tokens. The warning now leads with `CONFIG IGNORED`, names the exact file and error, and states that allowed_paths/tokens are NOT active. Tests: `tests/test_audit_fixes.py::test_unreadable_config_warns_not_silent` updated.
- **`[norm]` config now actually reaches the normalizer (dead-code audit B1).** `normalizer.configure()` had zero callers: `[norm].sort_lists` / `[norm].strip_strings` were plumbed through ENV_MAP (bool-coerced), the `_KNOWN` schema and the `Config.norm_*` properties, but every `normalize_tool_args()` call ran on the hard-coded module defaults — a user's TOML/env override changed nothing, cache keys stayed identical. `Config.__init__` now calls `normalizer.apply_config()` once per process (lazy import, no module-level consumer deps), so daemon, CLI, adapters and replay all honor the config; precedence is per-call args > programmatic `configure()` > config. **Behavior note:** MCP cache keys shift for users who had set these keys (previously ignored) — stale entries simply age out by TTL, no migration. Audit finding B2 (`recall.purge_expired` "prod-dead") needed no wiring: `garbage_collect()` already purges expired `recall_cache` rows inline (PRAGMA-guarded) on the daemon's 4h GC thread — the contract is now pinned so a refactor can't silently drop it. Tests: `tests/test_norm_config_wiring.py` (6: TOML + env → module default, precedence order, MCP-key end-to-end incl. the plain-JSON preimage when `[norm] enabled = false`) + `tests/test_gc_recall_purge.py` (2: expired-deleted/live-kept with exact rowcount, frozen-clock boundary pinning GC's strict `expires_at < now`).
- **Dead-code audit follow-ups (2026-09-13 review, fc03f10 bundle).** `_safe_bind_host()` removed from `transport.py` — the wildcard→loopback coercion lives in `_parse_tcp()` since the CodeQL bind-sink work, so the helper was a second (unreached) copy of the invariant; its wildcard-coercion test coverage migrated to `test_parse_tcp_coerces_all_wildcards` (empty/whitespace/`0.0.0.0`/`::` via `tcp://` paths) + `test_parse_tcp_keeps_specific_interface` — the security regression coverage is re-pointed at the live boundary, not deleted. `invalidate_all()`'s docstring no longer recommends `invalidate_api_host()` as an IPC-reachable path: per-host api_cache invalidation is programmatic-only (the `cache_invalidate` scope enum is `agent`/`api` only); the function itself stays public API per the 0.8.21 scope-aware-invalidate entry. Bench cleanup: 12 one-off scripts from the 2026-07 A/B session deleted (`fix.sed`, `final_fix.py`, `fix_clean.py`, `fix_imports.py`, `fix_rename.py`, `canon_v7_proto.py`, `probe_image_accept*.py`, `check_dbs.py`, `overnight_claude_bench.py`, `watch_and_analyze.py` — the last two also carried the `hermesbot`/`/home/hermes` identity strings from the push-scrub alert), plus `scripts/update.py` (second updater; `toolrecall/updater.py` is the one code path, audit F2). Operator scripts `scripts/savings.py` / `proxy_usage_query.py` / `tr_warp_stats.py` kept.

### Fixed
- **a6/e2e test portability: fixture TOML paths are now escaped (`tests/toml_helpers.py`).** Three test files f-stringed `tmp_path` (on Windows `C:\Users\...`) raw into TOML — `\U` is an invalid escape, tomllib raised, `Config._load` warned-and-fell-back to package defaults, and every `[network.clients."id"]` entry silently vanished. The a6 namespacing tests passed on Windows for the vacuous reason that all members shared the default namespace; on Linux the same fixtures genuinely exercised the feature. `write_toml_config()` now JSON-escapes every path (`json.dumps` escape set ⊂ TOML basic-string escapes) and parse-asserts the file at setup, so a broken fixture fails loudly instead of passing on defaults. Migrated: `test_a6_namespacing.py`, `test_a6_review_fixes.py`, `test_e2e_network_daemon.py`; contract pinned by `tests/test_toml_helpers.py` (8, incl. the exact `C:\Users` round-trip).

## [0.8.21] — 2026-09-13
### Added
- **MCP Tool Result Identity v1 — stable request keys for MCP bridge responses.** `toolrecall mcp` responses now carry a deterministic request key derived from the call payload via RFC 8785 (JCS) canonical JSON + SHA-256, so identical MCP calls are cache-identical across sessions and harmless drift (key ordering, whitespace, numeric spelling) cannot split the cache. Golden-file contract: `tests/golden_mcp_v1.json`. Tests: `tests/test_mcp_request_key_v1.py`.
- **Layer-0 keys: uniform lone-surrogate rejection + contentDigest preimage tag.** Text containing unpaired UTF-16 surrogates can survive `str` round-trips but breaks byte-level hashing (and SQLite storage) — key derivation now rejects lone surrogates uniformly at every entry point instead of failing late or diverging per call site. `content_digest` tags preimages explicitly so forensic hash mismatches are readable. Tests: `tests/test_keys.py`.
- **Replay fork primitives + GEPA adapter (`gepa` branch merge).** `toolrecall/replay` gains a node tree: `replay_tree_nodes`/`replay_tree_calls` tables, `fork_tree`/`fork_node`/`replay_at`/`record_call_at` (recursive-CTE ancestor chains, deepest-node-wins), scoped `_Mode` registry (per-branch record/replay without the global singleton), and `ContextTracker(branch_id=...)` persisting to a per-branch `tracker_state_branch` table — concurrent tree branches no longer cross-contaminate clean/dirty sets (G4). CLI: `toolrecall replay tree|tree new|fork|at`. New `toolrecall/adapters/gepa.py`: structural GEPAAdapter (zero deps, no gepa import) chaining candidates through forked branches — candidate k+1 replays the shared tool-call prefix from the tree and re-executes only divergent calls. Verified: real gepa 0.1.4 engine e2e (tests/run_gepa_e2e_standalone.py) and a billed fork-vs-naive A/B (bench/run_gepa_fork_ab.py, results in bench/results/GEPA_FORK_AB_RESULTS.md: 79% measured saving, 25/25 byte-exact HIT replays).
- **A6-7: per-scope write namespaces + provenance for the shared network daemon.** Remote member writes land under a scope namespace (`s:<sha256(allowed_paths)[:16]>` — identical scopes share, different scopes never collide); the GLOBAL namespace stays reserved for local (operator) traffic, so the team cache still warms from operator traffic while a compromised member can poison only their own scope. `private_writes = true` per `[network.clients."<id>"]` upgrades to full member isolation (`m:<id>`). Reads fall through own-ns → global (two-step lookup, memory + SQLite). Provenance: `access_log.client` + `file_cache.client`/`ns` columns (idempotent migrations, `''` = local/unknown) — "who wrote this entry" is now a DB query. `invalidate_file(ns=)` removes only that namespace's entry. Zero-deps held; local (UDS) behavior byte-identical (global ns, no client stamps). Closes review finding A6-5's isolation gap; SECURITY.md §9.4 rewritten. Tests: `tests/test_a6_namespacing.py` (11) + 144 network/file-cache back-compat tests.
- **Scope-aware `cache_invalidate` — the api_cache exclusion is now product behavior with an explicit opt-in, not a test comment.** `invalidate_all()` has never cleared `api_cache` (deliberate: its rows are billed LLM responses — wiping them re-bills every replayed call, and in-flight proxy stores re-insert immediately, so a full wipe is best-effort anyway), but its docstring claimed "Clear ALL caches" and the daemon replied "All ToolRecall caches cleared" — the contract lived only in a comment in `tests/test_proxy_canon_hermes.py`. The exclusion stays; the truth-telling is fixed: `invalidate_all()`'s docstring states scope + why; the daemon's default reply is honest ("api_cache untouched — resend with scope=api to include it"); `cache_invalidate(scope="api")` is the explicit opt-in that also wipes `api_cache` and returns the deleted row count (`api_entries_cleared`, money-tier visibility). New `invalidate_api_cache()` (full-table DELETE, returns rowcount) joins `invalidate_api_host()` in the public API; the MCP `cache_invalidate` tool schema documents the `scope` enum (`agent`/`api`); unknown scope values are rejected before any deletion (security gate still runs first). Client daemon-down fallback honors the scope (agent fallback never touches api_cache). Tests: `tests/test_invalidate_scope.py` (9: table-level contract, handler scopes incl. unknown-scope/gate-ordering, daemon-down fallback) + canon-hermes e2e per-test cleanup now runs the opt-in over real IPC instead of direct-DB surgery (`tests/test_proxy_canon_hermes.py`).
- **Tracker persistence across daemon restarts (`[tracker].persist_ttl`, default 21600s = 6h).** The context tracker's checkpoint/dirty/clean flow no longer resets every time the daemon restarts. `ContextTracker.snapshot()` serializes the full in-memory state (read set, dirty map, `_read_seq`/`_write_seq`, `_seq`, checkpoint counter, banked-once set, dropped-token total, armed flag) as one JSON blob into a `tracker_state` singleton table via the shared `_db()` connection; `restore()` reads it back at daemon boot. Gates: TTL (restoring yesterday's read set would describe a context the agent no longer holds — default 6h covers a working day, `0` disables restore), read-set **mtime re-validation** (files modified after the snapshot are dropped from the restored read set — a stale-mtime read would misreport clean/stale), deleted files pruned, sensitive-pattern paths filtered at restore (egress-side, matching `get_stale`'s egress rule). Written on graceful `stop()` BEFORE the WAL truncate-checkpoint (the row merges into the main DB — survives sidecar unlink) plus a 60s piggyback snapshot in the periodic-checkpoint thread (crash insurance: SIGKILL loses ≤60s of tracking, not the flow). `context_reset` also deletes the persisted row (reset means *forget* — a later restart must not resurrect state the agent dropped). Fail-soft everywhere: any snapshot/restore error leaves the fresh in-memory tracker untouched (swallow-audit-clean: no new except-pass sites). Registered: `[tracker]` section itself was NOT in the config `_KNOWN` schema (users setting `ignore_read_prefixes` got spurious unknown-section warnings) — now `{"ignore_read_prefixes", "persist_ttl"}`; env `TOOLRECALL_TRACKER_PERSIST_TTL`. Tests: `tests/test_tracker_persistence.py` (22: round-trip, TTL, mtime/delete/sensitive filtering, banked-counter stability, reset-clears-row, corrupt-DB fail-soft) + e2e `tests/test_e2e_tracker_persistence.py` (2: real stop→boot across two daemon subprocesses — read set survives; reset stays empty).
- **Forward-proxy request-body dedup (`[forward_proxy] dedup`, default OFF) — the dedup hook becomes a built-in proxy feature.** The proven LiteLLM-adapter algorithm (billing-verified **−32.3% prompt tokens / −30.0% cost** on SWE-bench Lite × 8 turns) now runs inside the forward proxy itself: `/v1/chat/completions` POST bodies have duplicate large text blocks (agent file re-reads) replaced with short stubs before forwarding, keep-first so provider-side prefix caching is untouched. Implementation: the dedup core moves to a new transport-neutral module `toolrecall.dedup` (no dependencies, import-guard preserved for LiteLLM-lit environments) and `toolrecall.adapters.litellm` re-exports it — public API unchanged, existing tests pass as-is. The proxy rewrites the body **after** `Content-Length` read and **before** streaming detection / canonicalization / cache key / forwarding, so all downstream steps see exactly the body the provider receives; streaming and non-streaming are both covered (streaming was previously impossible to serve through LiteLLM's hook path). Opt-in by design — the proxy never rewrites bodies silently: `[forward_proxy] dedup = true` in `toolrecall.toml`, env `TOOLRECALL_FORWARD_DEDUP` (mapped + bool-coerced in `config.ENV_MAP`), per-request escape hatch `X-ToolRecall-No-Dedup` (benchmark baseline arms use this). Knobs carry over: `TOOLRECALL_DEDUP_MIN_CHARS` (800), `TOOLRECALL_DEDUP_PROTECT_LAST` (2), `TOOLRECALL_DEDUP_MEDIA` (media dedup, default off). Failure of any kind forwards the original body (fail-open). `toml_serializer`'s starter config is now the importable `DEFAULT_CONFIG` (was inline in `__main__`) so key defaults are testable. **Live end-to-end verified (real OpenRouter billing through `:8569`):** two-turn conversation re-pasting a ~1,141-token tool result — turn 1 billed 1,123 prompt tokens, turn 2 billed **1,245** (vs 2,217+ undedupbed, measured in the same run), proxy log `DEDUP: stubbed 1 duplicate block(s), saved ~4564 chars`; keep-first prefix kept byte-identical so provider prompt-cache pricing still applied (turn 2 billed ≈ turn 1 despite 4 more messages). Tests: `tests/test_proxy_dedup.py` (18: unit, streaming + non-streaming HTTP e2e through a mock upstream, config-flag matrix, opt-out header, adapter-vs-proxy parity, multibyte round-trip; adapter suite green unchanged).
- **LangGraph adapter** — `toolrecall.adapters.langgraph`: a `BaseCheckpointSaver` implementation (`get_saver()` → `ToolRecallCheckpointSaver`) storing graph checkpoints in ToolRecall's cache (per-thread/per-namespace keys, survives restarts, `checkpoint_id` restores the exact node — the fork/resume primitive for tree-search/mutation-loop agents), plus `@cached_tool` for node tool calls (same contract as the ADK adapter; namespaced `langgraph`, async-safe, graceful bypass). Framework-free core (`_SaverCore`) + stdlib JSON serde; langgraph binds lazily, `pip install toolrecall[langgraph]` adds the extra. v1 limits documented (JSON-only default serde, logical `delete_thread`). Real-graph e2e: 5/5 scenarios on langgraph 1.2.11 (multi-node history+parents, interrupt/resume, time-travel rewind with tool executed once, restart persistence, subgraph namespaces). See `docs/TREE_SEARCH.md` for the tree-search design note this feeds.
- **`docs/TREE_SEARCH.md` design note** — tree search over agent executions (mutation loops): 3-axis cost model (billed input / attention pairs / real tool executions; all MODELED), prior-art gap analysis, phased contract (record_step/fork_branch/replay_to_node + workspace snapshot/restore), GEPA `batch_evaluate` integration point, explicit "what NOT to claim until the billed A/B" section.
- **Shared network daemon (A6) — team cache over TCP, opt-in, default OFF.** `[network]` in `toolrecall.toml` (`enabled`/`bind`/`port`/`token`/`tls`/`tls_cert`/`tls_key`/`clients`) + env parity (`TOOLRECALL_NETWORK_ENABLED/_BIND/_PORT/_TOKEN/_TLS/_TLS_CERT/_TLS_KEY`). Daemon: TCP listener alongside UDS (`127.0.0.1:8570`), HMAC-SHA256 challenge-response per connection (token never crosses the wire, single-use nonce cache, 3s window, explicit AUTH_ACCEPT so a rejection is distinguishable from a dead daemon), optional stdlib-TLS (`[network] tls=true`, CA-pinned client verification via `TOOLRECALL_NETWORK_TLS_CA`). Empty token = listener refuses to start. Remote authorization is default-deny: policy resolves ONLY from `[network.clients."<token-id>"]` (A4 machinery), the authenticated token-id overwrites `request["client"]` (no policy spoofing), and the remote flag travels per-request so UDS policy is unaffected. Client: `TOOLRECALL_TRANSPORT=tcp://host:port` + token env; remote selection is explicit-only (a token alone never flips the transport). Cross-machine cache semantics documented (file hits need a shared FS / identical checkout; terminal is cwd-scoped; API/MCP entries share fully) — ARCHITECTURE.md §3.3, SECURITY.md §9 threat model, README + DOCKER.md setup. `TOOLRECALL_CONFIG` env var now honored by `Config._load` (was documented in daemon/cli but ignored). Tests: `test_network_config` (14), `test_network_auth` (28), `test_network_tls` (7), `test_daemon_network` (11), `test_transport_network` (14), `test_remote_gate` (9), e2e `test_e2e_network_daemon` (4: shared-cache HIT local-write→remote-read, unauthenticated rejection, ping, wrong token).
- **`toolrecall top` TUI dashboard** — zero-dependency (stdlib `curses`) **interactive** dashboard: 5 views (stats, scrollable access log, daily hit-rate trend, context tracker with stale/dirty/clean file lists, help), selection + `Enter` to refresh a stale file (`cache_refresh_file`), `c` to set a context checkpoint — read-only otherwise. Talks to the daemon over UDS with an automatic read-only SQLite fallback for daemon-down/remote machines (WAL-without-shm DBs rescued via copy-snapshot read); non-tty runs print a single plain-text snapshot (`--once`). Arrow-key escape sequences handled (40ms peek). Wrong-user guard: when the local user has no cache data but another user's `~/.toolrecall/cache.db` exists, says so instead of an empty table. Metric-tier vocabulary in help (IO / CONTEXT / MONEY — don't conflate). Flags: `--interval`, `--db`, `--once`. Tests: `tests/test_tui_top.py` (11).
- **API proxy attachment for Hermes (setup step 5 + `toolrecall proxy attach|detach`)** — the billed-savings path: points Hermes' `llm.base_url` at the local forward proxy (`http://127.0.0.1:8569/v1`) so `api_cache` replay can capture provider usage fields (MONEY tier). Health-gated (`_proxy_alive()`: refuses to redirect LLM traffic to a dead proxy), opt-in (interactive prompt or `--yes`; non-interactive without `--yes` skips — attaching changes the agent's LLM failure mode, never silent), reversible (backup at `config.yaml.toolrecall-bak`; `proxy detach` restores byte-exact), idempotent. Text-level line edit, YAML structure untouched. Verified: sandboxed round-trip + dead-proxy refusal.
- **Metric vocabulary: three tiers, everywhere.** IO (hit rate, `tokens_served_from_cache/disk` — latency, NOT money) / CONTEXT (`tokens_not_sent_to_llm` — input tokens dropped from LLM context; 0 = checkpoint flow inactive, not "0 saved") / MONEY (billed input/output tokens — only from provider usage via the API proxy; zero data when traffic doesn't route through it). Daily-summary cron prompt + context script now carry mandatory metric rules (an earlier report wrongly labeled an IO ratio "2% token savings"); TUI help documents the tiers.

### Fixed
- **Security hardening F1/F14/F2 (shared-daemon attack surface).** **F1** — the in-allowed-dir symlink allowance is now an explicit, documented, configurable policy (`[security] allow_symlink_escapes`, env `TOOLRECALL_ALLOW_SYMLINK_ESCAPES`, default true = historical behavior) instead of an undocumented realpath bypass: realpath containment (Layer A) is always enforced, the lexical rescue (Layer B) is what the flag controls; residual risk documented in SECURITY.md 5. **F14** — fetch SSRF: URL validation resolves the host via getaddrinfo and blocks non-public destinations (loopback, RFC 1918, CGNAT 100.64/10, link-local/IMDS, reserved/multicast/unspecified, IPv6 ULA, IPv4-mapped IPv6, and numeric decimal/octal/hex IP spellings that `urlsplit` does not normalize); userinfo URLs rejected; redirects no longer followed (3xx surfaced to the caller); residual DNS-rebinding TOCTOU documented in SECURITY.md 1.F. **F2** — auto-updater supply chain: installs pinned to one index (`[update].index_url` > `TOOLRECALL_UPDATE_INDEX_URL` > official PyPI) with `--no-cache-dir`; pip can no longer discover extra indexes. Tests: `tests/test_regression_v078_v0711.py` (6 symlink-policy cases), `tests/test_proxy_ssrf.py` (hermetic fetch validation suite incl. numeric-IP cases), `tests/test_updater.py` (4 index-pinning cases incl. the actual `apply_update` pip invocation).
- **Forward-proxy stream tee: empty-stream guard — reasoning-only completions are no longer cached.** A degenerate but well-formed SSE stream (z-ai/glm-flash emitting only reasoning deltas, no content, no tool_calls, clean `[DONE]`) passed the old "[DONE] present" store guard and was tee-stored under its canonical hash — then replayed verbatim on every identical retry: measured live, 5× ct=0 HIT on one hash within 43 s (08:44:35–08:45:18) ending in a harness apperror; 66 such events lifetime, always in recurring bursts. Each burst = one poisoned entry the client kept replaying until give-up (plus the billed fallback re-STREAM after it). The tee's existing response reconstruction now doubles as the store guard: a stream with NO content and NO tool_call deltas is discarded (log warning, no `cached_api_store`). Reasoning deltas deliberately do NOT count as output — replaying them renders nothing user-visible (that was the failure); the retry needs a fresh provider turn. Truncation guard unchanged, tool-call-only streams still stored (the agent-harness norm), fail-soft behavior unchanged (any guard/store error lands in the existing best-effort except — warning, no store, no crash; no new except-pass sites). Tests: `tests/test_tee_empty_guard.py` (5: content stored / reasoning-only discarded / tool-call-only stored / truncated discarded / store-failure soft) + full proxy surface green (canon-hermes e2e 77 incl. stream-usage, dedup, tee-settle, transport).
- **Shim reads now count toward the CONTEXT tier when the content actually enters LLM context.** Every shim-served cache hit was tagged `source="shim"` → `context_tokens_saved` (`tokens_not_sent_to_llm`) ignored it, so on shim-installed agents the tier measured ~nothing while the shim IS the agent's file-read path. The fix tags a hit `agent_tool` only through a conservative AND-gate, fail-closed to `shim` (under-counting is a small understatement; over-counting would repeat the earlier inflation class): agent-process env marker (`HERMES_*`, `CLAUDECODE`, `CURSOR_TRACE_ID`, `AIDER_MODEL`, …), non-library path (`site-packages`/`node_modules`/`.venv`/`__pycache__`/`.git`/`vendor`/`.hermes` excluded — code the process runs, not code the model reads), non-binary suffix, size ≤ 256KB. Decided **pre-call** (path shape + one `os.stat`, never content) so cache stats stay exactly-once per `open()`. Override for unknown harnesses: `[shim].tag_source` = `always` / `never` / `auto` (default; env `TOOLRECALL_SHIM_TAG_SOURCE`, registered in `config._KNOWN` + `ENV_MAP`). Tests: `tests/test_shim_tagging.py` (23: gate matrix, routing — hit-in-agent-process → `agent_tool`, exactly-one-read-call regression guard — config plumbing).
- **Context tracker: banked-once dropped-token counter, honest INACTIVE state, ignore-listed agent-internal reads.** Three defects found and verified live: (1) `ctx_dropped_tokens_total` re-added the ENTIRE current clean set to the cumulative counter on every `context_set_checkpoint`/`context_get_dirty` call — N polls inflated the total N× the same clean set (proven live: 82.87M → 165.73M after a single `get_dirty`); each clean file is now banked exactly once. (2) The clean set was poisoned by daemon-side serve-through reads that were never in any agent context — 303 of 334 tracked files were WebUI session transcripts (~380MB) — so every "potential savings" figure was overstated by orders of magnitude; `[tracker].ignore_read_prefixes` (env `TOOLRECALL_TRACKER_IGNORE_PREFIXES`) now excludes agent session transcripts/cache paths by default (empty list = old behavior). (3) `toolrecall top` printed INACTIVE purely on `dropped == 0`, while the daemon's startup auto-checkpoint (`daemon_start`) banks 0 with an empty read set — a fresh daemon always read INACTIVE and checkpoint 1 did NOT mean the flow was armed; the tracker now carries an `agent_armed` flag (startup auto-checkpoint passes `armed=False`), and the TUI distinguishes INACTIVE / armed-nothing-banked / potential. `get_stats()` returns `agent_armed`; estimates remain bytes//4 (estimates, not tokenizer counts — not money). Tests: `tests/test_context_tracker.py` (bank-once regression guard incl. poll-stability, armed/disarmed/reset lifecycle, ignore-prefix + glob filtering; 27 unit + integration).
- **`tokens_read_from_disk` no longer inflated by ttl=0 reads** — never-cache reads (session transcripts: 3MB ≈ 775K tokens, rewritten every turn) were added to the cacheable miss denominator while being excluded from hits/misses, producing bogus savings ratios. New separate `tokens_read_uncacheable` bucket (schema + idempotent `_add_column` migration), exposed in `get_stats()`. Tests: `tests/test_uncacheable_bucket.py` (2).
- **`toolrecall top` Ctrl+C** exits cleanly (KeyboardInterrupt caught around `curses.wrapper`; terminal already restored by wrapper) instead of a traceback.
- **`toolrecall top` sqlite fallback survives WAL-without-shm** — `sqlite3.connect()` is lazy; the ro-open failure surfaced at first query (when the `-shm` index couldn't be created). `_open_ro()` now probes with a real `sqlite_master` read and falls back to a copy-snapshot (db + `-wal` into a writable temp dir), recovering committed-but-uncheckpointed WAL data without touching the original.
- **Docs: Claude Code / context-tracker positioning synced with verified platform docs.** CONTEXT_STALE.md no longer instructs readers to pass a `{path: tool_use_id}` map to Anthropic context editing — `clear_tool_uses_20250919` selection is threshold/keep-N only (`trigger`/`keep`/`clear_at_least`/`exclude_tools`; no per-ID targeting, no clean/dirty signal input). AGENT_COMPATIBILITY.md gains a "Why there is no fix from ToolRecall's side" section (hooks are forward-looking/observational only; API context editing is harness-owned), replaces a dead placeholder gist link with pointers to the in-doc evidence, and now states the constructive guidance: use Anthropic's native context editing in Claude Code, reserve the tracker for prunable harnesses. CONTEXT_TRACKER.md adds the native-context-editing comparison row and an append-only-harness limitation row; README + CONTEXT_TRACKER auto-hint wording corrected to match the checkpoint opt-in shipped in the 0.8.20 line (hints are NOT appended to "every tool response" — only after the client's first `context_set_checkpoint`; Claude Code receives zero hint text).

## [0.8.20] — 2026-09-06

### Added
- **Central logging rail (`toolrecall logging_setup`, default ON)** — daily-rotating file log (`~/.toolrecall/logs/toolrecall.log`, 7-day retention, `TOOLRECALL_LOG_MAX_DAYS`/`[log] max_days` to change; level `TOOLRECALL_LOG_LEVEL`/`[log] level`, default INFO). Payload-free by design: only paths, hashes, sizes, durations, outcomes — sensitive keys/payloads are redacted structurally (`redact()`) and secret-shaped free text is scrubbed by a last-line filter. Stdlib only (`dependencies=[]` preserved), fail-soft (unwritable path → no logging), hermetic-test safe, `propagate=False` so the host agent's logging is untouched. Wired into: daemon startup banner + per-request trace (`req=N cmd=… outcome=… dur=…ms`, previously invisible; unexpected handler crashes now log tracebacks instead of being silently swallowed), forward proxy (MISS→forward timing: status/dur/bytes), and the `mcp_github`/`mcp_fetch` MCP servers (replacing their non-rotating raw FileHandlers). New CLI verb `toolrecall logs [--tail N] [--level X]` to inspect the log. Tests: `tests/test_logging_setup.py` (10).

### Changed
- **`[sources].scan_dirs` default no longer `$HOME`** — the unconfigured default indexed the entire home directory (multi-GB junk DB; the 3.4 GB `knowledge.db` bloat incident). Default is now the curated, bounded pair `<agent_home>/memories` + `<agent_home>/skills`, resolved from the agent home (`AGENT_HOME` env → `[paths].agent_home` → `~/.hermes` fallback) — agent-agnostic, so any agent that sets `AGENT_HOME` gets its own memory and skills indexed. Explicit config always wins; `scan_dirs = []` disables home-scan indexing and auto-refresh entirely. Auto-refresh now also runs against the safe default (previously gated off for unconfigured installs).

### Added
- **Self-healing knowledge index** — the FTS knowledge DB stamps `last_index` on every indexing run; `docs_search()` against an index older than `[docs].index_ttl` (default 86400 s, env `TOOLRECALL_DOCS_INDEX_TTL`, `0` = off) triggers a background re-index that never blocks the query. Single-flight lock prevents parallel refreshes. Auto-refresh is disabled when sources are unconfigured (default `scan_dirs = [$HOME]`) to avoid background home-directory walks. Documented in `KNOWLEDGE_DB.md` / `CONFIG_REFERENCE`; tests in `tests/test_docs_refresh.py` (15).
- **Warp adapter + edge relay** — custom-inference-endpoint adapter for the Warp agent harness: full-chain tests, live public-URL rig (cloudflared quick tunnel + mock upstream), whitespace-tolerant edge auth, true SSE streaming relay (no full-body buffering), SSE keepalive during upstream silence, and `TOOLRECALL_EDGE_CANONICALIZE=auto` for edge-side warp profile application. Documented in `toolrecall/adapters/README.md`; ingress auth + trust model in `SECURITY.md` §8.
- **Stream cache for the forward proxy** — canonical-profile requests tee raw SSE into the `api_cache` and replay HITs as synthesized SSE: byte-exact passthrough framing (`Content-Length`, `[DONE]` terminal), tool-call delta reassembly, provider-shape-mirroring replay (content:null on tool-call deltas, created/model fields), truncated-stream rejection (no `[DONE]` → never cached or replayed). Canonicalization (CR normalization, blank-line collapse, whitespace-tolerant keys) applies to both streaming and non-streaming bodies, stabilizing keys across non-deterministic agent harnesses.
- **Proxy resilience** — retry with jittered backoff + circuit breaker on `_forward()`; request coalescing on the terminal miss path; stale-while-revalidate + adaptive TTL across terminal/MCP/API layers; per-path file TTLs (folder/filetype globs, trust windows, never-cache). Config keys and behavior documented in `docs/RESILIENCE.md` / `CONFIG_REFERENCE`.
- **Canonical command keys + fuzzy TTL classification** in the normalizer — equivalent commands collapse to one cache key regardless of flag order/spacing.
- **A7 cache regression gate** — cold/warm/invalidation bench harness on an isolated daemon (`make bench-gate`, CI workflow `ci(bench)`); warp bench rig v2 (shared-context, retry-probe, 10 instances, billing-verified costs) with energy + CO2e metrics and a claims-verdict table mapping every pitch claim to a measured row.
- **Opt-out auto-updater** — default ON, zero dependencies, the daemon never checks; `pipx`-aware upgrade path (`pipx upgrade toolrecall` instead of naked pip). Windows handover fixes for 0.8.19: setup crash, daemon lock, restart crash, cp1252.
- **Stats clarity** — token metrics renamed for attribution: `served_from_disk`/`served_from_cache` vs `not_sent_to_llm`; `tr-warp-stats` reports the identical-request fraction (and billed-token share) of any proxy usage log.

### Fixed
- **cwd-scoped terminal cache keys** — terminal output cache keys now include the working directory (constitution gap A1), preventing wrong-cwd cache hits.
- **Per-agent security policies** — `mcp_client_policy` gain for the A4 SecurityGate; constitution gaps A2/A3/A5/A8 closed.
- **Wildcard TCP host normalization** at the parse boundary (CodeQL bind-socket finding).
- **Daemon lifecycle** — instance-lock file unlinked on graceful shutdown; stale daemon locks swept; virtual-FS paths excluded from the shim; env-leak auto-starts stopped with dead-env-path override warnings.
- **DB migrations idempotent** via `PRAGMA table_info` — no new silent swallows; 3 pre-existing `except-pass` handlers narrowed.
- **Knowledge-index durability** — FTS self-heal no longer silently swallows rebuild failures; index compaction reclaims FTS bloat from re-index churn; curated default `scan_dirs` never index `$HOME`.
- **Warp canon** — CR normalization + blank-line collapse for edge canonicalization; edge upstream timeout raised 120s → 300s; HIT replay framing fixed (byte-exact `Content-Length`, `X-ToolRecall-No-Cache` escape hatch).

## [0.8.19] — 2026-08-24

### Added
- **Context recall tier (`toolrecall recall`)** — new optional stack, default-off gated on `[recall].enabled`: `recall_cache` schema table, `recall store/get` CLI + daemon `recall_store`/`recall_get` commands, MCP `recall_store`/`recall_get` tools, `recall_stats` with per-sink accounting, and a reproducible content classifier. Purpose documented in README / `RECALL_TIER.md` / `CONFIG_REFERENCE` with TTL-behavior tests.
- **TTL expiry for recall entries** — recall cache entries support time-to-live expiry; `docs(recall)` clarifies purpose, TTL behavior + tests.
- **Agent-agnostic `.pth` shim** — venv discovery + opt-in `.pth` shim (`toolrecall shim`) now works across agents; CLI reference + agent-type mechanism matrix added (`--venv`/`--all`/`--status`/`--uninstall`).
- **Generalized healthcheck** — `toolrecall healthcheck` generalized; persistent shim disable/enable.
- **`make` bench/docker/validate targets** — `make` gains bench/docker/validate targets; uv dev-deps + mypy gating fixed.
- **Cross-platform setup** — relative DB-path robustness + cross-platform setup hardening.

### Fixed
- **Shim no longer intercepts `subprocess.run`/`subprocess.Popen` (Option B)** — the shim now patches only `builtins.open` for read-only `r`/`rt` access (keyed on path + mtime + size); terminal output runs natively in the agent's process, never transparently cached at the shim layer. `cached_shell_exec`/`cached_terminal` remain as explicit daemon-side calls (gated by `SecurityGate.check_terminal`). Regression tests: `tests/test_shim.py::TestOptionBNoSubprocess`.
- **Shim serves cache hits as real files + size-aware invalidation** — `fix(shim)`.
- **Socket divergence fixed** — shim + transport layer.

### Changed
- **CodeReview/security** — security scan (shell=True/eval/exec, `/etc`/`/dev` allowlist), doc/architecture audit, pre-commit hygiene: all clean.
- **MCP multiplexer docs** — removed orphaned `end` in mermaid diagram, defined `dedup` correctly; ARCHITECTURE table row fixed; LiteLLM standalone hook added to adapter docs.

## [0.8.18] — 2026-08-13

### Security
- **`cached_shell_exec` now gated by `SecurityGate.check_terminal`** — the daemon's `cached_shell_exec` dispatch previously executed commands with **no** terminal allowlist check (unlike `cached_terminal`), so a client could run shell commands even with `allow_terminal=false` or outside `allowed_terminal_commands`. It now routes through `_handle_shell_exec`, which strips agent wrappers and gates the inner command identically to `_handle_terminal`. Regression tests: `tests/test_shell_exec_gate.py`.
- **Shipped `config.toml` defaults hardened** — removed broad `allowed_paths` entries `/etc` and `/dev` (now `["~"]` only) and set `allow_terminal = false` (opt-in). Also fixed a pre-existing TOML section-placement bug so `allow_terminal`, `allow_invalidate`, `emit_context_hints`, and `allowed_terminal_commands` actually parse under `[mcp]` where the code reads them.

### Added
- **`toolrecall.adapters.litellm`** — LiteLLM Gateway Hook: stubs byte-identical duplicate large text blocks per request (keep-first, `protected_tail=2`, fails-open). Measured on **10 real SWE-bench Lite instances × 8 accumulated turns (80 requests/arm, DeepSeek V4 Flash via OpenRouter, billing-verified)**: **32.3% fewer prompt tokens** (417,256 → 282,688) and **30.0% lower billed cost** ($0.0191 → $0.0134), with no prefix-cache damage (effective $/M $0.0474 vs $0.0459). Savings are a curve that grows with session length (0% turns 1–3, ~50% by turn 8).

### Changed
- **README token-savings wording corrected** — removed the synthetic harness's "73–91%" and "~97%" request-token figures (those came from a codegen loop, not real tasks). The only billing-verified figure stated is the LiteLLM Gateway Hook's **32.3%/30%** from real SWE-bench Lite. File / Terminal Cache row now describes bounded context growth qualitatively without an unverified percentage.

### Fixed
- **TCP transport no longer binds to all interfaces** — `bind_socket` now coerces any wildcard host (`''`, `0.0.0.0`, `::`) to loopback `127.0.0.1` before binding, so a `tcp://0.0.0.0:port` or `tcp://:port` path can never expose the IPC socket to the network (CodeQL `py/bind-socket-all-network-interfaces`). New `_safe_bind_host()` helper; regression tests in `tests/test_transport.py`.
- **Daemon single-instance lock** — `run_daemon` now acquires an atomic `flock` guard keyed on the socket path before binding. Previously `bind_socket` unconditionally unlinked/rebound the UDS, so repeated `toolrecall daemon` calls could stack orphaned instances (all listening on fd 6) — the leaked-daemon bug seen in the field. flock serializes racing starts (second one exits with "already running"), is auto-released on process death (no stale-lock trap), and is per-socket so tests with temp sockets keep independent locks. Regression tests: `tests/test_daemon_pid_guard.py::TestDaemonSingleInstanceLock`.
- **Test isolation / skip guards** — `test_e2e_adk_adapter.py` and `test_e2e_langchain_adapter.py` now `pytest.importorskip` their optional deps (google-adk, langchain) instead of failing when not installed; `test_adapters.py` skips the langchain-dependent test the same way; `test_shim.py` lambdas re-accept the `source='shim'` kwarg `_shim_open` passes (regression from 78ad6c17); `test_context_tracker.py`'s `_check_daemon` fixture now skips on the `{"error": "daemon_unavailable"}` dict that `send()` returns when no daemon is up (previously treated the error dict as a live response).
- **`_CachedPopen` context-manager support** — the shim's cached-Popen wrapper now implements `__enter__`/`__exit__`. `subprocess.run()` internally uses `with Popen(...) as process:`, so when the shim routed a cached command through `_CachedPopen` it crashed with `'_CachedPopen' object does not support the context manager protocol`. This only surfaced when a daemon was running (shim active); with no daemon it fell back and passed, making it look flaky. Regression tests: `tests/test_e2e_shim.py`.
- **`test_client.py` env restore** — `TestClientFallbackDirect.tearDown` now restores `TOOLRECALL_MCP_ALLOWED_PATHS` / `TOOLRECALL_SCAN_DIRS`. A test set the allowlist env var but never restored it, leaking a stale allowlist that pointed every later `E2EDaemon` child at a dead tmpdir (they rejected their own fresh temp dirs with `Path not allowed: access denied`) — the cause of the order-dependent `test_e2e_cache_socket` failures in a full run.

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
- **Domain migration to toolrecall.dev complete** — URL migration applied across LICENSE, Dockerfile, pyproject.toml, README, configs, docs.

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
