# Appendix

## A. ToolRecall vs Claude Code

Both cache file reads but at fundamentally different layers.

| | Claude Code (default) | ToolRecall |
|---|---|---|
| **Recommendation** | Default state tracking is sufficient alone | ⚠️ **Do not enable file/terminal caching** — use only for MCP multiplex + forward proxy |
| **Cache scope** | Per-session (in-memory) | Cross-session (SQLite disk + in-memory LRU) |
| **What is cached** | `read_file` calls only | Files, terminal output, skills, docs, MCP |
| **Persistence** | Lost when `claude` exits | Survives reboots, daemon restarts, agent switches |
| **All agents share?** | No — isolated per CLI session | Yes — single daemon serves Hermes + OpenCode + Cline |
| **Dependencies** | Node.js + 100MB+ | Zero — pure Python stdlib (~132 KB install) |
| **MCP multiplex** | Each server = new subprocess | Single daemon, lazy-load (~0.01s), idle timeout 15min |
| **Auto-heal** | Manual restart | systemd + watchdog + IPC shutdown/restart |
| **Security** | OS-level only | WAF: path allowlist, `.env` air-gap, terminal blackhole, cognitive scan |

**Cost comparison:** 13-file project, 10 re-reads, 100 turns per session → 81% fewer file-read tokens, 67% faster MCP startup, ~$40 saved per 100 sessions.

## B. CTO Questions & Resolutions

**1. Cache invalidation & state drift.** Three separate answers: (a) Virtual/ephemeral FS (Docker, NFS) — mtime misses fall back to live execution, no correctness loss; (b) Non-file tool outputs — never cached by default; only 8 static commands cached (hostname, whoami, pwd, uname, crontab, df, uptime, free); (c) Hidden dependencies — ToolRecall caches exact tool outputs, not semantics. Dependency-aware invalidation is an LLM-layer problem.

**2. Race conditions & multi-agent concurrency.** Inherited from OS — `open()+read()` is not atomic with or without ToolRecall. Split-brain state is an agent orchestration bug, not cache-layer bug. Mitigations: `bypass_cache=True`, `cache_invalidate()`, per-file invalidation.

**3. Security & data leakage.** Cross-tenant contamination is valid if deployed incorrectly on shared CI — but ToolRecall is designed for developer workstations. Mitigations: per-job DB isolation (`TOOLRECALL_CACHE_DB=/tmp/isolated-$ID.db`), or disable daemon. Cache poisoning requires filesystem access the attacker already has. Cached outputs are deterministic — no timestamps, PIDs, or user names in API payloads (reduces leakage vs live execution).

**4. Architectural fragility ("below Layer 4").** ToolRecall does NOT monkey-patch OS primitives. No LD_PRELOAD, no ptrace, no FUSE, no import hooks. It is a standard MCP server. Only MCP protocol changes can break it.

**5. Observability.** Every response includes `{"cached": true}` and `X-ToolRecall-Cache: HIT` header. Honest gap: most observability tools don't surface the flag prominently — best fixed at agent framework level.

**Three real risks:** (1) Don't share cache DB across CI jobs, (2) TTL caches serve stale data by design — use only for static commands, (3) Observability won't surface `cached` flag without instrumentation.

## C. Latency Pitch

ToolRecall saves **1 hour 25 minutes** of wait time in a 13-hour session by collapsing tool execution latency.

**Local execution:** 827 cache hits × ~1.5s = **~20.6 min saved** (subprocess skip). Cache hit: ~0.6ms vs ~1.5s live → ~2500× on that path.

**API context (TTFT):** Lean context avoids ~10s/TTFT per bloated turn. 386 turns × 10s = **~64 min saved**.

**Total: ~85 min reclaimed.**

**Trade-offs:** (1) Cache invalidation is hard — `mtime` binding breaks on modified files bypassing sanctioned tools; (2) Non-deterministic data (stock prices, logs) → `ttl=0` required; (3) RAM vs tokens: ~8–11 MB idle, 2.1 MB SQLite DB, spikes to ~130 MB when MCP servers load, then idle-back to 11 MB.

## D. OSI Model Analogy

ToolRecall sits at **Layer 4 (Transport/Daemon)** in the agent tool execution stack — the same layer regardless of agent or MCP protocol.

| Layer | Without TR | With TR (Hit) |
|---|---|---|
| L7 — Agent | Calls tool | Calls tool (no change) |
| L6 — Tool Protocol | `read_file main.py` | Same MCP call (no change) |
| L5 — Session/IPC | UDS to daemon | Same socket (no change) |
| **L4 — Daemon (GATE)** | Forwards to OS | **Returns from SQLite LRU (~0.6ms)** |
| L3–L1 — OS/HW | subprocess + disk I/O (~1.5s) | **Skipped entirely** |

**Key insight:** Layers 5–7 are identical with or without TR. Layers 1–3 are completely bypassed on cache hits. ~1000× speedup comes from removing the subprocess, not optimizing it.

## E. O(N²) Context Snowball Theory

**The problem:** Every agent turn appends all previous tool output to context. O(N²) attention cost: Turn 1 = 1K tokens (1M pairs), Turn 50 = 50K tokens (2.5B pairs). This is economically destructive — each `read_file` re-read inflates cost quadratically.

**Solution:** ToolRecall intercepts tool calls at the OS layer and serves byte-identical cached responses from local SQLite — **tool execution cost goes from O(N²) to O(1) per cache-hit call.**

**Real-world impact (13-hour benchmark, Hermes + Gemini 3.1 Pro):**

| Metric | Without TR | With TR |
|---|---|---|
| Tool calls served | 0 | **827** (666 file, 143 terminal, 10 mcp) |
| Cache hit rate | 0% | **89%** (file: 91%) |
| Tool latency | ~1.5s/subprocess | **~0.6ms** (daemon) |
| Wait time | ~20 min waiting | ~0.5s total cache-hit |
| Unique content cached | 0 bytes | **~64,889 bytes** (13 files) |
| Tokens (3× re-read) | ~204K | ~55K → **73% fewer** |
| Provider prefix-caching | No | **Yes** → up to **90% discount** |

**Token interception (corrected v0.3.2):** Original 141M tokens was a double-counting bug. Real unique: ~64,889 bytes (~21,630 tokens). At 3× re-read: ~204K → ~55K = 73% reduction. At 10×: ~630K → ~55K = ~81% reduction.

**Knowledge DB:** FTS5-indexed agent memory instead of injecting everything into prompt. Same deterministic contract: O(1) lookup vs O(N) context injection. <1.5ms per query.

**A2A Swarm Multiplier:** First agent pays I/O cost, swarm benefits from shared SQLite WAL. Total: 1× I/O, 0 additional context bloat.

## F. Cost Projections (ROI)

Two distinct mechanisms:

**1. Local deduplication (measured).** 13 files, 3× re-reads, ~55K unique tokens. At $3/M tokens: ~$0.17 saved per workload. Scales linearly with re-reads.

**2. Provider prefix-caching discount (90%).** This is the larger lever — applies to **every API call**, not just repeated tool reads. Byte-identical payloads qualify for Anthropic/OpenAI's up-to-90% discount (automatic, no config).

| Scenario | Sessions | Calls | Total Tokens | No Discount | 90% Discount | **Savings** |
|---|---|---|---|---|---|---|
| Single deep session | 1 | 1,000 | 20M | $60 | $6 | **$54** |
| Daily (1 month) | 22 | — | 440M | $1,320 | $132 | **$1,188/mo** |
| Solo dev (annual) | 264 | — | 5.28B | $15,840 | $1,584 | **$14,256/yr** |
| Team of 100 (annual) | 26,400 | — | 528B | $1,584,000 | $158,400 | **$1.43M/yr** |

**Engineering time:** ~20 min saved per heavy session → 200 sessions/yr = ~67 hr annual → team of 100 = ~6,700 hr → ~$502K/yr at $75/hr.

**Infrastructure:** 5 MCP servers without TR = ~600MB/agent session; with TR = ~11MB idle, ~130MB active (shared across sessions, zero additional cost).

## G. Vision & Roadmap

**Beyond agent caching — use cases:**
- **CI/CD pipelines:** mtime-based cache → 10–50× faster cache-hit steps on unchanged files
- **LLM inference (vLLM, TGI):** In-memory LRU for hot model configs (~0.001ms), SQLite persistence for warm (~7ms)
- **ETL pipelines:** Static reference data → <1ms instead of seconds
- **Static site generators:** Per-file mtime → only parse changed files
- **Microservice API caching:** TTL-based, same pattern as Redis/memcached but zero infra
- **IDE/LSP plugins:** Zero-dependency requirement matters — no vendoring heavy cache libs

**Accidental wins:**
- **Air-gapped offline mode:** Cached MCP responses work without network
- **Hot-path detection:** SQLite hit counts reveal which files agents struggle with
- **Zero-penalty context switching:** ~0.6ms latency means no cost to drop/reacquire context
- **100% ecosystem penetration via MCP stdio**

**v0.8.12 roadmap delivered:** (1) Context Tracker auto-hint + MCP bridge auto-trigger, (2) Replay mode for CI/CD, (3) Hermes transparent cache via OS-level .pth shim, (4) Forward proxy streaming, (5) MCP multiplexer with idle timeout, (6) Context drop detection via daemon ping, (7) `ctx_dropped_tokens` tracking in healthcheck, (8) 560+ test suite.

**Beyond v0.6.0:** Multi-tenant team gateway → shared VPC cache → Developer B gets Developer A's cache milliseconds later. Synthetic data flywheel → frozen trajectories for DPO training. Empirical alignment via deterministic `[Intent → Action → OS Observation → Human Correction]` pairs. High-speed RL (AlphaGo paradigm for OS agents) → train against cache instead of physical OS time.

**Enterprise scale:** 500 turns of 1M-token codebase with byte-identical outputs → 90% discount on every turn. 24/7 CI/CD fleet (10 agents × 500 PRs/day × 200K tokens) → cache eliminates redundant reads. Rate-limit immunity: API queried once, frozen locally.

**GDPR:** File reads never leave machine, agents drop sensitive files after use, zero telemetry.

## H. Security Audit

**OWASP Top 10:2021 + LLM Top 10:2025 audit** (Semgrep SAST, 678 rules, credential scan, injection analysis, path traversal).

| Severity | Count |
|---|---|
| 🔴 Critical | 1 |
| 🟡 High | 3 |
| 🟡 Medium | 5 |
| 🟢 Low | 4 |

**🔴 C1 — HTTP proxy CORS:** `Access-Control-Allow-Origin: *` on ALL endpoints (including `/cache/invalidate`). Default bind is `127.0.0.1` (safe) but port 8569 is unauthenticated. **Status:** Fixed — CORS headers removed. Proxy binds to localhost only.

**🟡 H1 — Shell injection:** 5 `shell=True` calls in `cache.py`, 3 with variable `args` → `shlex.split()` primary path, `shell=True` fallback. **Fix:** Remove `shell=True` fallbacks in `cached_run()`; log WARNING on fallback.

**🟡 H2 — GitHub token leak:** `mcp_github.py:123` writes `TOKEN[:8]...` to stderr on startup. **Fix:** Replace with `"Token: configured"`.

**🟡 H3 — MD5 for cache keys:** Not a security boundary (collision corrupts cache only). **Fix:** Low priority — document as non-cryptographic usage.

**🟡 Medium findings:** Token in process listing (`/proc/PID/environ`), null-byte bypass (`daemon.py:115`), module-level stderr leak on import (`mcp_github.py:25-28`), `chmod(0o700)` on socket (single-user only), log handler at module scope.

**🟢 Low:** No rate limiting on UDS, Python `.venv/` path, test files in `/root/`, no TLS on TCP fallback.

**Already good:** Default-deny path allowlist, 25+ sensitive file regexes, `os.path.realpath()` everywhere, air-gapped secrets in `~/.toolrecall/.env`, parameterized SQL, 1MB/5MB limits, 176+ security tests, no `curl | bash` install.

**Top 5 fixes:** (1) ~~Remove CORS `*`~~ ✅ Done, (2) Stop token leak in stderr, (3) Add null-byte rejection, (4) Lazy init log handler, (5) Log `shell=True` fallback.

## I. Windows Compatibility

|| | Linux | Windows |
|---|---|---|---|
| Transport | UDS (`/run/user/.../toolrecall.sock`) | TCP `127.0.0.1:8568` |
| Daemon | `os.fork()` | `multiprocessing` spawn |
| Auto-start | `systemctl --user` | manual / scheduled task (no systemd) |
| Crash restart | systemd `Restart=on-failure` + watchdog | Watchdog only (Popen fallback) |

**Auto-detection:** `transport.py` checks `IS_WINDOWS = (sys.platform == "win32")` → TCP fallback. Proxy bind always `127.0.0.1` (never `0.0.0.0`). Port random via OS.

**Known footguns:** (1) Python not on PATH → fix with `$env:Path += ";...\\Python312\\Scripts"`; (2) 260-char MAX_PATH → handled via `\\?\` prefix + Python 3.11+; (3) Backslash vs forward slash → `os.path.realpath` normalizes both; (4) No socket cleanup on TCP → `SO_REUSEADDR` avoids TIME_WAIT; (5) Antivirus → max 1-2s delay, retry or add exclusion.

**Daemon crash fixes (root cause):** ThreadPoolExecutor created before `os.fork()` → corrupted locks on child process → moved to `start()` (post-fork). `start()` now wraps in `try/except BaseException` + `faulthandler.enable()`. YAML parser fixed: 2/4-space indent detection, proper `[1:-1]` bracket stripping, `try/except` on all parses.

**Auto-healing:** systemd (~2s), watchdog (~10s).