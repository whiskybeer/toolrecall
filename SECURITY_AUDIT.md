# Security Audit — ToolRecall (whiskybeer/toolrecall)

**Date:** 2026-06-11
**Scope:** Full OWASP Top 10:2021 + OWASP LLM Top 10:2025 audit covering all Python source, scripts, configs, infrastructure
**Method:** Manual code review, Semgrep SAST (678 rules), credential scan, injection analysis, path traversal audit

---

## Summary

| Category | Status |
|----------|--------|
| 🔴 Critical | 1 |
| 🟡 High | 3 |
| 🟡 Medium | 5 |
| 🟢 Low/Observation | 4 |

---

## 🔴 CRITICAL (1)

### C1. HTTP Proxy: `Access-Control-Allow-Origin: *` on UDS-Forwarded Cache

**Location:** `toolrecall/proxy.py:112`
**Risk:** The HTTP proxy (used by HTTP-only agents like Claude Code, Codex) sets `Access-Control-Allow-Origin: *` on ALL endpoints, including `/cache/invalidate`, `/cache/invalidate_file`, and `/cached_terminal`. If the proxy is bound to anything other than `127.0.0.1` (user changes config), any website could trigger cache invalidation or terminal execution via a user's browser (CSRF).
**Mitigation in place:** Default bind is `127.0.0.1` (safe). Port 8567 is unauthenticated though.
→ **Fix:** Remove CORS `*` entirely (`Access-Control-Allow-Origin` is unnecessary for localhost-only services). If CORS is needed, add an explicit `Access-Control-Allow-Origin: null` or validate `Origin` header.

---

## 🟡 HIGH (3)

### H1. Shell Injection Surface — `subprocess.run(shell=True, ...)` with Untrusted Args

**Location:** `toolrecall/cache.py:593`, `:624`, `:663`, `:669`, `:703`
**Risk:** 5 `shell=True` calls, 3 of which accept variable input (`args` parameter from callers). `cached_run(script_path, args)` passes `f"{path} {args}"` to a shell — if `args` contains `; rm -rf /`, it executes on the host.
**Mitigation in place:** `cached_terminal()` uses `shlex.split()` as primary path with `shell=True` as fallback. Code-level docs describe the safe path. Only called through MCP which validates `allow_terminal`.
→ **Fix:** Remove the `shell=True` fallbacks in `cached_run()` — the `shlex.split()` path handles all cacheable scripts. For `cached_terminal()`, the `shell=True` fallback on line 624 is the only path for complex commands — log it with WARNING level so admin can monitor usage.

### H2. GitHub Token in MCP Server `serverInfo` Response

**Location:** `toolrecall/mcp_github.py:123`, `:171`
**Risk:** The MCP `initialize` response leaks `"security": {"token_local": True}` in `serverInfo` — not the token itself, but confirms the server has a token. More critically, line 123 writes `TOKEN[:8]... (N chars)` to stderr on every startup. Any MCP client that captures stderr sees `Token: ghp_abc... (40 chars)` — leaking 8 chars of the PAT.
→ **Fix:** Remove line 123 (`system.stderr.write(f"  Token: ... \n")`). Replace with `sys.stderr.write("  Token: configured\n")`. The `serverInfo` `token_local` flag is acceptable (boolean only).

### H3. MD5 for Cache Keying — Non-Cryptographic but Inconsistent with SECURITY.md Claims

**Location:** `toolrecall/cache.py:366, 402, 596, 672, 734, 948, 954, 1097`; `toolrecall/docs.py:250`
**Risk:** 9 `hashlib.md5()` calls used for cache key hashing. MD5 is not collision-resistant but cache keys are not a security boundary — a collision would only corrupt the cache, not compromise security. However, this contradicts the SECURITY.md's cryptographic posture.
→ **Fix:** Low priority. Document in SECURITY.md that MD5 is used for cache keying only (not security). Optionally migrate to `hashlib.sha256().hexdigest()[:16]` for a cleaner posture. No functional impact either way.

---

## 🟡 MEDIUM (5)

### M1. Token Displayed in Process Listing

**Location:** `toolrecall/mcp_github.py:33`, `toolrecall/daemon.py:426-431`
**Risk:** The `GITHUB_TOKEN` is stored in `HEADERS["Authorization"]` and remains in memory for the MCP subprocess lifetime. While not written to disk, a `/proc/PID/environ` or debugger attack could extract it. The daemon passes env vars to subprocesses via `subprocess.Popen(env=full_env)` — the token is in the subprocess environment.
**Mitigation:** This is standard daemon architecture — tokens in daemon env, isolated subprocess. No user-visible exposure.
→ **Fix:** Acceptable risk. Document in SECURITY.md as known: MCP subprocesses inherit the daemon's env. 

### M2. Sensitive File Blocklist Bypass — Absence of Null-Byte Check in daemon.py

**Location:** `toolrecall/daemon.py:115-147` (check_path)
**Risk:** The `check_path` method checks `len(path) > MAX_PATH_LENGTH`, then uses `os.path.realpath()` for allowlist checking. Neither explicitly rejects null bytes (`\x00`). While Python's `os.path.realpath()` does raise `ValueError` on null bytes in CPython, the check occurs AFTER the length check — but `len()` counts past the null byte, which could cause a bypass if the path passes length check but null byte tricks the OS layer on some platforms.
→ **Fix:** Add `if '\x00' in path: return "Path not allowed: invalid characters"` BEFORE the length check in both `daemon.py` and `cache.py`. Matches the `cache.py` layer's behavior.

### M3. Module-Level Stderr Leak on Token Absence

**Location:** `toolrecall/mcp_github.py:25-28`
**Risk:** At module import time (not in `main()`), the script checks `if not TOKEN` and writes to `sys.stderr`. This fires on ANY import of `toolrecall.mcp_github`, not just active use. If imported for test discovery or tool listing, it pollutes stderr unnecessarily.
→ **Fix:** Move the token check into `main()` — only warn on actual server start, not module import.

### M4. Log File Handler at Module Scope

**Location:** `toolrecall/mcp_github.py:13-21`
**Risk:** `logging.FileHandler` is instantiated at module level. On import, it opens the log file for appending. Every import opens a new file handle (inherited by all forked processes). Matches the known memory anti-pattern: module-level IO that fires on every import.
→ **Fix:** Move log handler setup into `main()` or a lazy `_setup_logging()` function called once.

### M5. `chmod(0o700)` on Socket — Documentation Inconsistency

**Location:** `toolrecall/transport.py:85`
**Risk:** The UDS socket is created with `0o700` permissions (owner read/write/exec only). On multi-user systems, other users on the same machine cannot access the daemon socket. However, `0o700` on the socket prevents OTHER users from connecting but doesn't prevent any process running as the same user from connecting. On a single-user setup (VPS), this is fine.
→ **Fix:** Acceptable for single-user. Document in SECURITY.md for multi-user deployments: consider `0o600` and restrict directory permissions.

---

## 🟢 LOW / Observations (4)

### L1. No Rate Limiting on UDS Daemon

The daemon processes requests over UDS with no rate limiting. An agent in a tight loop calling `cached_read` could flood the daemon. However, the agent's own input token cost provides natural rate limiting — there's an economic ceiling to request flooding.

### L2. Python `.venv/` in OS Path Search Order

The `scripts/update.py` uses `os.path.dirname(os.path.abspath(__file__))` to detect repo root, then runs pip from it. No path injection risk (no user input in paths).

### L3. Test Files Write to `/root/.toolrecall/`

**Location:** `tests/uninstall-test-setup.py:24-59`
Test setup helper writes files to `/root/.toolrecall/` — harmless for test environments but shows the test assumes root. Tests use env vars to override paths, so this is just a test hygiene issue.

### L4. No TLS for TCP Fallback (Windows)

On Windows, `transport.py` falls back to TCP on `127.0.0.1:8568` with NO TLS. This is localhost-only, but any process on the same machine could connect. Acceptable for dev tools; document for enterprise deployments.

---

## OWASP Top 10:2021 — Per-File Audit Grid

| File | Lines | A01 | A02 | A03 | A04 | A05 | A06 | A07 | A08 | A09 | A10 |
|------|-------|-----|-----|-----|-----|-----|-----|-----|-----|-----|-----|
| cache.py | 1140 | ✅ | ⚠️ | ⚠️ | ✅ | ✅ | ✅ | ✅ | ✅ | 🟡 | ✅ |
| daemon.py | 1148 | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | 🟡 | ✅ |
| proxy.py | 157 | 🟡 | ✅ | ✅ | 🟡 | ✅ | ✅ | ✅ | ✅ | 🟡 | 🟡 |
| mcp_github.py | 193 | ✅ | 🟡 | ✅ | ✅ | ✅ | ✅ | ⚠️ | ✅ | 🟡 | ✅ |
| mcp_server.py | 400+ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | 🟡 | ✅ |
| transport.py | 189 | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | 🟡 | ✅ |
| config.toml | 152 | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | 🟡 | ✅ |

Legend: ✅ = pass, ⚠️ = partial, 🟡 = informational, 🔴 = critical

---

## OWASP Top 10 for LLM Applications 2025

| ID | Category | Status | Notes |
|----|----------|--------|-------|
| LLM01 | Prompt Injection | 🟡 | WAF + allowlist + blocklist + cognitive scan mitigate, but no sandbox |
| LLM02 | Sensitive Info Disclosure | 🟡 | Cache DB unencrypted on disk, tokens in MCP subprocess env |
| LLM03 | Supply Chain | ✅ | Zero external runtime deps (stdlib), 100% auditable |
| LLM04 | Data Poisoning | ✅ | No fine-tuning, no embeddings, deterministic FTS5 |
| LLM05 | Improper Output Handling | ✅ | Cached outputs are byte-identical, no output processing |
| LLM06 | **Excessive Agency** | 🟡 | Default-deny MCP, terminal OFF by default, path allowlist required — but agent is on bare host |
| LLM07 | System Prompt Leakage | 🟡 | `serverInfo` leaks `token_local: True` flag — not content but confirms token existence |
| LLM08 | Vector Weaknesses | ✅ | FTS5 (deterministic), not embeddings — no adversarial doc injection |
| LLM09 | Misinformation | 🟡 | Cached stale data possible if agent doesn't re-read (mtime invalidation prevents this in cache layer) |
| LLM10 | Unbounded Consumption | ✅ | 5MB file limit, 1MB message limit, 30s terminal timeout, implicit token-cost ceiling |

---

## Trust Boundary Assessment

```
┌─────────────────────────────────────────────────────────────────┐
│  AGENT (untrusted — assumed compromised by prompt injection)     │
│  → Can call: cached_read, cached_terminal (if enabled),          │
│    cached_mcp (multiplexer servers)                              │
│  → CANNOT: access ~/.toolrecall/.env (daemon manages secrets)    │
│  → CANNOT: read outside allowed_paths                            │
│  → CANNOT: read .env / .ssh / *.pem (blocklist)                  │
└─────────────────────────────────────────────────────────────────┘
                              │ UDS / MCP
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│  DAEMON (trusted — runs as user, manages secrets)                │
│  → Reads .env, manages MCP subprocesses, SQLite cache            │
│  → Validates ALL requests through SecurityGate                   │
│  → enforce_path() → enforce_terminal() → enforce_invalidate()    │
│     → enforce_mcp_tool_access()                                  │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│  MCP SUBPROCESSES (trusted — isolated stdio, inherit env)       │
│  → mcp_github: has GITHUB_TOKEN in env                          │
│  → mcp_time: no secrets                                         │
│  → mcp_fetch: no secrets                                        │
└─────────────────────────────────────────────────────────────────┘
```

---

## Top 5 Fixes (Priority Order)

1. **🔴 Remove `Access-Control-Allow-Origin: *` from HTTP proxy** — proxy.py:112 → delete or replace with origin validation
2. **🟡 Remove token leak in MCP server stderr** — mcp_github.py:123 → `Token: configured` instead of `TOKEN[:8]...`
3. **🟡 Add null-byte rejection** — daemon.py:115 + cache.py → `if '\x00' in path:` before allowlist check
4. **🟡 Move module-level logging handler to lazy init** — mcp_github.py:13-21 → `_setup_logging()` called from `main()`
5. **🟡 Add WARNING log on `shell=True` fallback** — cache.py:624 → log when `shlex.split()` fails and shell=True is used

---

## Already Good (What's Done Well)

- **Default-deny path allowlist** — when `allowed_paths` is empty, NO paths are readable
- **Sensitive file blocklist** — 25+ regex patterns covering `.env`, `.ssh`, `.aws`, `.pem`, credentials files
- **`os.path.realpath()` everywhere** — symlink and `../` traversal resolved before allowlist check
- **Air-gapped secrets** — tokens live in `~/.toolrecall/.env`, managed by daemon, never in agent context
- **UDS only, no TCP** — daemon binds to Unix Domain Socket, immune to SSRF and remote scanning
- **1MB message limit + 5MB file limit** — prevents OOM attacks via streaming or oversized reads
- **`allow_terminal = false` by default** — terminal execution is opt-in
- **`allow_invalidate = false` by default** — cache invalidation is opt-in
- **Cognitive scan** — regex patterns block credential fishing and context-overflow injection attempts
- **Parameterized SQL everywhere** — no f-string SQL, `WHERE path_hash = ?` consistently
- **176+ security tests** — `test_security_waf.py`, `test_security_injection.py`, `test_ast_security.py`, `test_cognitive_scan.py`, `test_cache_safety.py`
- **No `curl | bash` install** — pip install only, setup.sh is for git clone users
- **SECURITY.md documents limitations** — honest assessment of what the tool does and doesn't protect against
