# ToolRecall Test Suite

## Quick Start

```bash
cd /path/to/toolrecall

# All unit tests (fast, ~5s)
python3 -m pytest tests/ -v -m "not e2e" --tb=short

# All e2e tests (slower, ~20s — spawns real daemon)
python3 -m pytest tests/ -v -m "e2e" --tb=short

# Everything
python3 -m pytest tests/ -v --tb=short

# Single file
python3 -m pytest tests/test_cli.py -v --tb=short
```

## Test File Overview

| File | Tests | Category | What It Covers |
|------|-------|----------|----------------|
| `test_cli.py` | — | CLI | CLI commands, argparse, setup flow |
| `test_transport.py` | 22 | IPC | UDS/TCP socket lifecycle, framed protocol, TransportClient |
| `test_client.py` | 27 | IPC | daemon-first routing, fallback, singleton management |
| `test_mcp_bridge.py` | 22 | MCP | MCP JSON-RPC protocol, security gate filtering, tool definitions |
| `test_mcp_fetch.py` | 6 | MCP | stdlib fetch server validation |
| `test_mcp_github.py` | 6 | MCP | GitHub MCP server (token auth, pagination) |
| `test_mcp_time.py` | 10 | MCP | Time MCP server protocol |
| `test_mcp_seqthink.py` | 17 | MCP | Sequential Thinking MCP server |
| `test_mcp_registry.py` | 16 | MCP | server registry resolution (built-in vs uvx) |
| `test_mcp_config.py` | 6 | MCP | MCP config parsing |
| `test_mcp_config_resolve.py` | 7 | MCP | config auto-resolution |
| `test_file_cache.py` | 3 | Cache | file cache hit/miss, mtime invalidation, 5MB OOM protection |
| `test_write_cache.py` | — | Cache | write-through cache behavior |
| `test_cache_safety.py` | 6 | Cache | cache TTL behavior |
| `test_mcp_transparent_cache.py` | — | Cache | transparent cache mode for MCP |
| `test_memory_index.py` | 19 | Index | memory indexing, FTS5, BM25, directory indexing |
| `test_toml_serializer.py` | — | Config | TOML serialization round-trip |
| `test_security_waf.py` | 4 | Security | WAF: dangerous tool blocking, directory traversal |
| `test_security_injection.py` | 12+ | Security | OWASP injection: SSTI, null byte, error leakage, bypass |
| `test_ast_security.py` | — | Security | AST-level security analysis |
| `test_context_tracker.py` | — | Context | context dropping, micro-RAG |
| `test_cognitive_scan.py` | — | Scan | cognitive complexity scanning |
| `test_daemon_pid_guard.py` | — | Daemon | PID file guard, duplicate start prevention |
| `test_regression_v078_v0711.py` | — | Regression | regression checks for v0.7.8 → v0.7.11 |
| `test_integration.py` | 9 | E2E | index → FTS5 → get_page pipeline |
| `test_e2e_daemon_lifecycle.py` | 4 | E2E | daemon start/ping/stop/restart via real Unix socket |
| `test_e2e_cache_socket.py` | 4 | E2E | cached_read hit/miss/mtime invalidation + stats |
| `test_e2e_client_daemon.py` | 2 | E2E | client daemon_running() True/False |
| `test_e2e_cli.py` | 2 | E2E | CLI via subprocess (daemon --foreground) |
| `test_e2e_stress.py` | 2 | E2E | 10 concurrent requests, 5x rapid restart |

**Total:** 550+ tests across 38 files (as of v0.8.10).

## Naming Conventions

| Prefix | Purpose | Daemon Required? |
|--------|---------|:---:|
| `test_e2e_*` | End-to-end — spawns real daemon subprocess over UDS | ✅ Yes |
| `test_mcp_*` | MCP server protocol and logic | ❌ No |
| `test_cache_*` | Cache hit/miss, TTL, invalidation | ❌ No |
| `test_file_*` | File I/O specific tests | ❌ No |
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

1. Create `tests/test_<feature>.py`
2. Import from repo root via `sys.path.insert(0, ...)`
3. Mark E2E tests with `@pytest.mark.e2e`
4. Each test MUST document **WHAT** it tests and **WHY** (threat model)
5. Run: `python3 -m pytest tests/test_<feature>.py -v --tb=short`

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