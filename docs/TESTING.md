# Testing Guide — Test Philosophy, Organization, and Coverage

## Test Runner

```bash
# Full suite
python -m pytest tests/ -v

# Single file
python -m pytest tests/test_mcp_fetch.py -v

# By keyword
python -m pytest tests/ -k "registry"
```

**Requirements:** Python 3.11+ stdlib only — no test dependencies beyond `pytest`.

**Current count:** 328 tests, all passing (as of v0.7.5).

## Test File Organization

Each test file covers a distinct module or feature:

### Core Cache Layer

| File | Module Under Test | What It Tests |
|------|------------------|---------------|
| `tests/test_cache.py` | `toolrecall/cache.py` | LRU eviction, SQLite persistence, mtime invalidation, `get_stats()`, `invalidate_all()`, path expansion, hash algorithm switching (MD5 → SHA256) |
| `tests/test_write_cache.py` | `toolrecall/cache.py` (write/patch) | `cached_write` (skip identical content, large file bypass), `cached_patch` (apply, skip applied, skip not-found, write-then-patch integration) |

### Daemon & IPC

| File | Module Under Test | What It Tests |
|------|------------------|---------------|
| `tests/test_daemon_pid_guard.py` | `toolrecall/daemon.py` | PID file lifecycle: create, prevent duplicate daemon, stale PID cleanup, kill on conflict, permission errors |
| `tests/test_transport.py` | `toolrecall/transport.py` | UDS socket lifecycle, TCP fallback, framed message protocol (large messages, message boundaries, pings, timeouts, daemon-unavailable handling) |

### Client & Proxy

| File | Module Under Test | What It Tests |
|------|------------------|---------------|
| `tests/test_client.py` | `toolrecall/client.py` | Daemon IPC (ping, send, receive), fallback to direct SQLite, error handling |
| `tests/test_proxy.py` | `toolrecall/proxy.py` | HTTP proxy caching by body hash, cache hit/miss responses, header passthrough, error handling |

### MCP Layer

| File | Module Under Test | What It Tests |
|------|------------------|---------------|
| `tests/test_mcp_registry.py` | `toolrecall/mcp_registry.py` | Server resolution (`resolve_server`, case-insensitive, unknown server), builtin vs external detection, `list_registered_servers()`, `has_uvx()`, `is_known()`, `is_builtin()` |
| `tests/test_mcp_config_resolve.py` | `toolrecall/config.py` (auto-resolution) | `mcp_multiplex_servers_config` property with registry resolution, explicit `servers_config` override, unknown server skip, partial override, env var overrides |
| `tests/test_mcp_fetch.py` | `toolrecall/mcp_fetch.py` | Module imports, TOOLS list, URL validation (valid/invalid schemes, localhost), handler dispatch, `MAX_CONTENT_BYTES` defaults, env var override, negative value handling, registry integration (fetch is builtin) |
| `tests/test_mcp.py` | `toolrecall/mcp_bridge.py` | MCP JsonRpc protocol (initialize, tools/list, tools/call), error handling, response formatting |

### Security

| File | Module Under Test | What It Tests |
|------|------------------|---------------|
| `tests/test_cognitive_scan.py` | `toolrecall/security.py` | 70 injection patterns (override instructions, jailbreak tags, exfiltration URLs), 50 legitimate patterns (code, configs, logs) — 86% detection rate |
| `tests/test_ast_security.py` | `toolrecall/security.py` | AST injection detection: `exec()`, `eval()`, `__import__()`, `compile()`, dynamic imports, function redefinition — plus false-positive suppression for safe patterns |

### Knowledge Base & Docs

| File | Module Under Test | What It Tests |
|------|------------------|---------------|
| `tests/test_docs.py` | `toolrecall/docs.py` | FTS5 index operations (create, search, delete), memory indexing, `docs_search()` BM25 relevance, multi-source filtering |

### Context Tracker

| File | Module Under Test | What It Tests |
|------|------------------|---------------|
| `tests/test_context_tracker.py` | `toolrecall/context_tracker.py` | Checkpoint lifecycle, dirty/clean tracking, mark_dirty on write/patch, reset, get_stats, concurrent access |

### Hermes Integration

| File | Module Under Test | What It Tests |
|------|------------------|---------------|
| `tests/test_hermes_init.py` | `toolrecall/hermes_init.py` | import behavior, mode detection, tool registration, transparent mode monkey-patching, daemon-running detection |

## Test Philosophy

1. **No external dependencies.** Tests should run without network, without API keys, without a running daemon. All daemon tests use a temporary UDS socket.
2. **Deterministic.** No random data, no time-dependent assertions, no flaky tests. If a test fails, it's a real bug.
3. **Isolated.** Each test creates its own temporary files/databases. No test should depend on another test's side effects.
4. **Coverage over perfection.** A simple test that exercises the happy path is worth more than a perfect test that catches every edge case.
5. **Integration at module boundaries.** Pure unit tests for logic (registry resolution). Integration tests for daemon IPC (start a real daemon, call it).

## Key Source Files

| Test File | Tests Module | Lines |
|-----------|-------------|-------|
| `test_mcp_fetch.py` | `toolrecall/mcp_fetch.py` | 120 |
| `test_mcp_registry.py` | `toolrecall/mcp_registry.py` | 162 |
| `test_mcp_config_resolve.py` | `toolrecall/config.py` | 200 |
| `test_daemon_pid_guard.py` | `toolrecall/daemon.py` | 354 |

## See Also

- [CLI Reference](CLI.md) — how to run tests via CLI commands
- [Configuration Reference](CONFIG_REFERENCE.md) — test configs use the same `config.py` loader
- [MCP Multiplexer](MCP_MULTIPLEXER.md) — `mcp_registry.py` tests reference the registry module