# ToolRecall Benchmark — 2026-06-07

## Session Context

- **Date:** 2026-06-07, 09:33–22:30 UTC (~13 hours)
- **Model:** deepseek/deepseek-v4-flash via OpenRouter
- **Provider:** OpenRouter
- **Environment:** GCP e2-medium (4GB RAM, 4GB swap), Hermes Agent on Telegram
- **ToolRecall version:** 0.3.0 (MCP Multiplexer with lazy start)

## Token Savings

| Cache Layer | Hits | Misses | Hit Rate | Tokens Saved | Est. Cost Saved |
|---|---|---|---|---|---|
| file_cache | 666 | 62 | **91%** | 141,105,842 | ~$282 (@$2/M in) |
| terminal_cache | 143 | 15 | **91%** | 1,220 | ~$0 |
| code_cache | 8 | 9 | **47%** | 4,757 | ~$0 |
| mcp_cache | 10 | 18 | **37%** | 254 | ~$0 |
| **TOTAL** | **827** | **104** | **89%** | **141,112,165** | **~$282** |

## Cache Efficiency

- **Overall hit rate:** 89% — 8 of every 9 cache lookups were served from cache
- **file_cache dominates** (99.9% of savings) — the session reads many files repeatedly
- **mcp_cache** is new (v0.3.0 MCP Multiplexer); hit rate will improve with cross-session data

## Architecture Impact (v0.3.0 MCP Multiplexer)

| Metric | Before (eager, per-session) | After (lazy, daemon-managed) |
|---|---|---|
| Daemon RAM (idle) | — | **11 MB** |
| Daemon RAM (active, 1 server) | — | **~130 MB** |
| Daemon RAM (all 5 servers) | ~3.6 GB (6× 600MB per session) | **~600 MB** (one-time) |
| MCP server startup per session | ~1.7s (6 processes) | **~0.01s** (UDS connect) |
| First call latency | — (already running) | **~1.4s** (lazy start) |
| Idle resource recovery | never | **15-minute idle timeout** |

## Daemon Health

- **Uptime:** Daemon runs persistently as a systemd user service
- **Socket:** `/run/user/1004/toolrecall.sock` (XDG_RUNTIME_DIR, no /tmp)
- **Watchdog:** Every 10 minutes via cron, silent until threshold exceeded
- **Server pool:** github (26 tools), time (2), fetch (1), hermes-docs (2), sequential-thinking (1)

## Key Learnings

1. **Lazy start is critical** — 490MB → 11MB on daemon boot
2. **Idle timeout is critical** — Node-based MCP servers (github, sequential-thinking) consume ~80MB each even when idle
3. **Cross-session persistence** — the daemon survives Hermes restarts, so cache hit rates compound
4. **MCP multiplexer caching** — second identical call is instant (0.01s vs 1.4s)
