# ToolRecall Test Suite

## Quick Start

```bash
cd /path/to/toolrecall

# All unit tests (fast, ~80s)
python3 -m pytest tests/ -v -m "not e2e" --tb=short

# All e2e tests (slower — spawns real daemon subprocesses)
python3 -m pytest tests/ -v -m "e2e" --tb=short

# Everything
python3 -m pytest tests/ -v --tb=short

# Single file
python3 -m pytest tests/test_cli.py -v --tb=short
```

Plain `pytest` collects only `tests/` (`testpaths` is set in pyproject.toml) —
benchmark scripts under `bench/` are never swept up.

## Hermeticity (read before writing tests)

`tests/conftest.py` makes every test hermetic by default:

- **Env isolation (autouse)** — `TOOLRECALL_CACHE_DB`, `TOOLRECALL_KNOWLEDGE_DB`,
  and `BENCH_DB_PATH` point at per-test temp files; `TOOLRECALL_DOCS_INDEX_TTL=0`
  disables the background reindex thread (it resolves DB paths at thread-execution
  time, i.e. after teardown, and races the next test).
- **Production-path guard (autouse)** — wraps `sqlite3.connect` and
  `socket.socket.connect`; any touch of `~/.toolrecall`, `/run/user/*/toolrecall.sock`,
  or `:8569` FAILS the test. Exempt via `@pytest.mark.allow_prod_paths` or
  `_PROD_EXEMPT_FILES` (with a justifying comment).
- **Silent-swallow audit** (`test_silent_swallow_audit.py`) freezes the package's
  `except: pass` inventory — a new bare swallow breaks CI. Narrow + log the
  exception, or update `FROZEN_COUNTS` in the same commit with a justification.
- **Child venvs / subprocess pythons**: strip `PYTHONPATH`, set `PYTHONNOUSERSITE=1`,
  and assert `__file__` resolves inside the child's site-packages (see
  `test_shim_installed_pkg.py` for the pattern).

Note: tests using the `capsys` fixture cannot run under `-p no:capture`
(the `make test-unit` flag). Use plain `-q`/`-v` invocations for those files.

## Test File Overview

| File | Tests | Category | What It Covers |
|------|-------|----------|----------------|
| `test_adapters.py` | 27 | Adapters | LiteLLM/LangChain adapter behavior, TTL plumbing |
| `test_ast_security.py` | 19 | Security | AST-level injection: exec/eval/import/def blocking, perf |
| `test_cache_safety.py` | 8 | Cache | cache TTL behavior |
| `test_cli.py` | 9 | CLI | CLI commands, argparse, setup flow |
| `test_client.py` | 28 | IPC | daemon-first routing, fallback, singleton management |
| `test_cognitive_scan.py` | 25 | Security | cognitive role-hijack/exfil scanning, perf |
| `test_context_stale.py` | 9 | Context | stale-file eviction correctness |
| `test_context_stale_security.py` | 16 | Security | stale-eviction security guarantees |
| `test_context_tracker.py` | 31 | Context | context dropping, micro-RAG |
| `test_daemon_env_hygiene.py` | 6 | Daemon | startup env warnings |
| `test_daemon_pid_guard.py` | 8 | Daemon | PID fallback, single-instance flock lock |
| `test_db_concurrency.py` | 6 | Cache | parallel writers, torn reads, lock-leak probes (`_db` RLock/refcount) |
| `test_docs_compact.py` | 4 | Index | FTS index compaction thresholds |
| `test_docs_refresh.py` | 15 | Index | freshness stamping, TTL, background refresh, single-flight |
| `test_e2e_cache_socket.py` | 4 | E2E | cached_read hit/miss/mtime invalidation + stats |
| `test_e2e_cli.py` | 1 | E2E | CLI via subprocess (daemon --foreground) |
| `test_e2e_client_daemon.py` | 2 | E2E | client daemon_running() True/False |
| `test_e2e_daemon_lifecycle.py` | 4 | E2E | daemon start/ping/stop/restart via real Unix socket |
| `test_e2e_proxy.py` | 9 | E2E | forward proxy end-to-end |
| `test_e2e_recall.py` | 8 | E2E | recall tier over the daemon |
| `test_e2e_shim.py` | 12 | E2E | shim interception via real daemon |
| `test_e2e_stress.py` | 2 | E2E | 10 concurrent requests, 5x rapid restart |
| `test_fault_injection.py` | 12 | IPC | transport boundary faults: refused/hang/EOF/RST/oversize/garbage |
| `test_file_cache.py` | 6 | Cache | file cache hit/miss, mtime invalidation, OOM protection |
| `test_healthcheck.py` | 11 | Daemon | healthcheck output logic |
| `test_integration.py` | 13 | E2E | index → FTS5 → get_page pipeline |
| `test_libsql_local.py` | 24 | Storage | libSQL embedded backend |
| `test_libsql_sync.py` | 11 | Storage | Turso sync opt-in |
| `test_mcp_bridge.py` | 40 | MCP | MCP JSON-RPC protocol, security gate, tool definitions |
| `test_mcp_config.py` | 6 | MCP | MCP config parsing |
| `test_mcp_config_resolve.py` | 7 | MCP | config auto-resolution |
| `test_mcp_fetch.py` | 13 | MCP | stdlib fetch server |
| `test_mcp_github.py` | 6 | MCP | GitHub MCP server |
| `test_mcp_registry.py` | 16 | MCP | server registry resolution (built-in vs uvx) |
| `test_mcp_seqthink.py` | 15 | MCP | Sequential Thinking server |
| `test_mcp_time.py` | 10 | MCP | time server protocol |
| `test_memory_index.py` | 19 | Index | memory indexing, FTS5, BM25 |
| `test_normalizer.py` | 30 | Cache | semantic-arg normalization for cache keys |
| `test_properties.py` | 17 | Property | hypothesis fuzzing: normalizer, toml_serializer (tomllib oracle), path_utils |
| `test_proxy_ssrf.py` | 6 | Security | SSRF guards in forward proxy |
| `test_recall.py` | 21 | Recall | recall tier roundtrip, dedup, GC, daemon gate |
| `test_recall_ttl_boundary.py` | 6 | Recall | exact expiry boundaries (frozen clock), sub-second TTL |
| `test_regression_v078_v0711.py` | 24 | Regression | v0.7.8 → v0.7.11 regressions |
| `test_replay.py` | 36 | Replay | replay mode |
| `test_security_injection.py` | 15 | Security | OWASP injection: SSTI, null byte, error leakage, bypass |
| `test_security_waf.py` | 4 | Security | WAF: dangerous tool blocking, directory traversal |
| `test_setup_platform.py` | 6 | Setup | cross-platform setup |
| `test_shell_exec_gate.py` | 4 | Security | shell-exec security gate |
| `test_shim.py` | 30 | Shim | open()/subprocess interception |
| `test_shim_installed_pkg.py` | 2 | Shim | shim from real wheel install in throwaway venv (packaging guard) |
| `test_shim_regressions.py` | 8 | Shim | size-aware invalidation regressions |
| `test_silent_swallow_audit.py` | 3 | Meta | frozen except-pass inventory (AST scan) |
| `test_toml_serializer.py` | 43 | Config | TOML escaping (incl. 0x7f/None regressions), round-trip |
| `test_transport.py` | 26 | IPC | UDS/TCP lifecycle, framed protocol |
| `test_turso_optin.py` | 16 | Storage | Turso opt-in semantics |
| `test_venvs.py` | 10 | Setup | venv detection/install |
| `test_warp_adapter.py` | 20 | Adapters | Warp adapter |
| `test_warp_fullchain.py` | 1 | E2E | Warp full chain |
| `test_write_cache.py` | 16 | Cache | write-through cache |

**Total:** ~806 collected tests across 59 files (v0.8.19+). Roughly 624 unit +
37 skipped in the default `make test-unit` selection (the rest are e2e/adk).

## Naming Conventions

| Prefix | Purpose | Daemon Required? |
|--------|---------|:---:|
| `test_e2e_*` | End-to-end — spawns real daemon subprocess over UDS | ✅ Yes |
| `test_mcp_*` | MCP server protocol and logic | ❌ No |
| `test_cache_*` | Cache hit/miss, TTL, invalidation | ❌ No |
| `test_security_*` | WAF, injection, OWASP compliance | ❌ No |
| `test_*` (other) | Unit tests for specific modules | ❌ No |

## E2E Tests

The E2E tests in `test_e2e_*.py` spawn a **real daemon subprocess**
and communicate over a **real Unix domain socket**. No mocking,
no patching. Each test gets its own temporary socket and its own
cache database.

**Note:** E2E tests are slower (~0.5–2s per test due to daemon startup).
They are tagged with `@pytest.mark.e2e` and excluded from the fast
run via `-m "not e2e"`.

### E2E Test Helper

`tests/e2e_helpers.py` provides `E2EDaemon` — a context manager that
spawns a daemon, waits until ready, and shuts down cleanly:

```python
from tests.e2e_helpers import E2EDaemon

with E2EDaemon() as d:
    result = d.client.send({"cmd": "ping"})
    assert result.get("pong")
```

## Adding New Tests

1. Create `tests/test_<feature>.py` — check existing suites first; extend
   rather than duplicate (fault injection, db concurrency, TTL boundary,
   property tests, shim-installed-package)
2. Import from repo root via `sys.path.insert(0, ...)`
3. Mark E2E tests with `@pytest.mark.e2e`
4. Each test MUST document **WHAT** it tests and **WHY** (threat model)
5. Rely on the autouse hermeticity fixtures — opt OUT only via
   `@pytest.mark.allow_prod_paths` with a justification
6. Run: `python3 -m pytest tests/test_<feature>.py -v --tb=short`

### Test docstring standard

```python
def test_foo():
    """Verify that foo behaves correctly when bar is baz.

    Why: A regression where foo returned None instead of []
    when bar=baz, causing callers to crash on .append().
    """
```

See `references/testing-conventions.md` in the ToolRecall skill for the
full testing guide (mock UDS server, daemon-first fallback, cross-test
isolation, and the 9-point pre-commit checklist).
