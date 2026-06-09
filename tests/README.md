# ToolRecall Test Suite

> **Tests:** 50+ unit tests covering cache, MCP keyword access control, MCP servers, and security.
> **Run:** `pytest tests/ -v` inside the toolrecall venv.

---

## Quick Start

```bash
cd /path/to/toolrecall
source .venv/bin/activate

# Run all tests (fast — ~1 second)
python3 -m pytest tests/ -v

# Run with verbose logging from tests
python3 -m pytest tests/ -v -s

# Run only one test file
python3 -m pytest tests/test_security_waf.py -v

# Run with debug-level logging
python3 -m pytest tests/ -v --log-cli-level=DEBUG
```

---

## Test Files

| Test File | Tests | What It Covers |
|-----------|-------|----------------|
| `test_integration.py` | 9 | **End-to-end pipeline**: memory index → FTS5 search → get_page, directory index → source-filtered search, multi-source isolation, re-index after delete, 100-entry stress test, file cache hit/miss/invalidation, index_all with config |
| `test_cache_safety.py` | 6 | Cache TTL behaviour: default TTL=0 means no caching, explicit TTL enables cache, dynamic commands not cached |
| `test_file_cache.py` | 3 | File cache hit/miss, mtime-based invalidation, file size limit (5MB OOM protection) |
| `test_security_waf.py` | 4 | MCP keyword access control: dangerous tool blocking, safe tool allowing, access control disable, directory traversal |
| `test_security_injection.py` | 12+ | OWASP injection vectors: SSTI, null byte, buffer overflow, error message leakage, bypass attempts, cache poisoning |
| `test_mcp_time.py` | 10 | Time MCP server: initialize, tool listing, get_time (UTC/GMT/EST/PST/unknown), list_timezones, missing args |
| `test_mcp_seqthink.py` | 17 | Sequential Thinking MCP: think_step depth/caching/hedging/questions, analyze contradictions, validate_reasoning, protocol compliance |
| `test_mcp_github.py` | 6 | GitHub MCP: 5 tool schemas, token detection, no-token warning on stderr, JSON-RPC error handling |
| `test_mcp_config.py` | 6 | TOML config parsing: all 3 Python servers individually and combined, mixed npx+Python, real config file validation |
| `test_memory_index.py` | 19 | **Hermes memory indexing** + **index_directory**: §-delimited parsing, FTS5 search, BM25 ranking, Porter stemming, triggers, custom source labels, directory indexing, custom extensions, .gitignore, re-index stability |

---

## Using the Security Tests

The security tests (`test_security_waf.py` and `test_security_injection.py`) use `SecurityGate` from `toolrecall.daemon` with a mock config. They test **logical correctness** of the access control — not the network layer.

### What They Prove

| Test | Attack | What It Verifies |
|------|--------|-----------------|
| `test_sandbox_blocks_dangerous_tools` | `execute_bash`, `delete_table` | Keyword filter blocks tool names containing dangerous verbs |
| `test_sandbox_allows_safe_tools` | `read_file`, `list_issues` | Safe tools pass through |
| `test_directory_traversal_waf` | `../../etc/passwd` | `os.path.realpath()` canonicalization blocks traversal |
| `test_ssti_injection_in_path` | `read_file("{{config}}")` | Path allowlist rejects template injection |
| `test_null_byte_poisoning` | `valid.png%00/etc/passwd` | Null bytes don't bypass path checks |
| `test_error_leaks_no_real_path` | read secret file | Error message says "Path not allowed", not "/etc/shadow blocked" |
| `test_cache_invalidate_blocked` | `cache_invalidate("all")` | DoS via cache purge is prevented by default |

### Test Logging

The test files print structured logs to stderr when run with `-s`:

```bash
python3 -m pytest tests/test_security_injection.py -v -s
```

Example output:
```
tests/test_security_injection.py::TestSecurityInjectionA03::test_ssti_injection_in_path 
  [SECURITY] === A03: Injection Attack Vectors ===
  [SECURITY] Testing SSTI path injection: read_file('{{config}}')
  [SECURITY]   PASS: Path rejected — ToolRecall MCP Access Control blocked tool...
```

To see all log output without assertion passthrough:
```
python3 -m pytest tests/ -v -s --tb=short 2>&1 | grep -E "(SECURITY|PASS|FAIL)"
```

---

## Running the Sandbox Shell Tests

The sandbox tests require Docker access:

```bash
cd /path/to/hermes-sandbox-setup
sg docker -c "bash test_sandbox.sh"
```

This runs 20 tests covering pool lifecycle, warm/cold exec, exit codes, parallel contention (6 parallel execs, pool size 3), and security isolation (network=none, read-only rootfs).

---

## Adding New Tests

1. Create `tests/test_<feature>.py`
2. Import `toolrecall.*` modules (tests run from the repo root via pytest)
3. Use `self.setUp()` to create fresh state per test
4. Print debug info with `print("  [TAG] message", file=sys.stderr, flush=True)` for `-s` mode
5. Run: `python3 -m pytest tests/test_<feature>.py -v`

### Naming conventions

| Prefix | Purpose |
|--------|---------|
| `test_cache_*` | Cache hit/miss, TTL, invalidation |
| `test_file_*` | File I/O specific tests |
| `test_security_*` | Access control, injection, OWASP compliance |
| `test_mcp_*` | MCP server protocol and logic |
| `test_*` | General (catch-all) |

---

## CI Integration

All tests produce exit code 0 on success:

```bash
python3 -m pytest tests/ -q --tb=short && echo "ALL PASSED"
# Expected: 50 passed in 0.90s
```

For GitHub Actions:
```yaml
- name: Run tests
  run: |
    source .venv/bin/activate
    pytest tests/ -v --tb=short --junitxml=report.xml
```