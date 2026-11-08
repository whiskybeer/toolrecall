# Cache write path: expired-row reclaim + checkpoint off the commit path

Branch `perf/cache-write-path`. Two changes, both about what the daemon's writes
cost. Follows `PERF_ACCESS_LOG_PRUNE.md`, which fixed the per-read prune; this
one takes on the two residuals that remained after it.

Measured on the live daemon (daemon log, 2026-09-26): after the prune fix the
median read was fast (p50 1.00 ms, 87.6 % under 10 ms) but ~12 % of reads sat at
10–100 ms and one 32 s stall occurred in 5.75 h. Neither was the prune.

## A2 — 1.09 GB of the DB was expired rows nothing ever reclaimed

`api_cache` rows expire after `API_CACHE_TTL` (300 s). `garbage_collect()` pruned
`terminal_cache`, `mcp_cache` and `recall_cache` — **`api_cache` was missing from
that list**, and the only other `api_cache` deletes in the codebase are a
full-table wipe and a per-host wipe, neither a TTL purge. So expired rows
accumulated forever.

| on a snapshot copy of the live DB | |
|---|---|
| `api_cache` rows | 12,385 |
| of which already expired | **12,281 (99.2 %)** |
| body bytes held by expired rows | **1,103 MB** |
| DB file | 1,189.9 MB → **85.9 MB** after purge + VACUUM |
| reclaimed | **1,105 MB — 13.9× smaller** |
| one-time cost | 65.8 s purge + 14.2 s VACUUM, all under the DB lock |

Fix: `DELETE FROM api_cache WHERE expires_at IS NOT NULL AND expires_at < ?` in
`garbage_collect()`, PRAGMA-guarded exactly like the `recall_cache` purge (a
pre-migration DB may lack the column, and a missing column must not abort the
whole GC — it would also skip the VACUUM that returns the space). Steady state is
a few hundred rows per 4 h cadence.

**Operational note:** the first GC run after this change pays the backlog once —
65.8 s of purge under the lock. On a busy instance do that initial reclaim during
a maintenance window instead of letting a live GC thread hold the lock for a
minute.

**Guardrail:** `[cache] api_cache_max_body_mb` (default 2 MB, cross-referenced as
`cache.API_CACHE_MAX_BODY_MB` / `config.API_CACHE_MAX_BODY_MB_DEFAULT`) bounds
what a single row can add. Oversize bodies are **skipped whole, never truncated**
— a truncated body would be replayed as a hit and would be a corrupt response to
the caller.

## A1 — the WAL checkpoint was folding into commits

`storage/sqlite.py` set `journal_mode=WAL`, `synchronous=NORMAL`, `busy_timeout`,
and left `wal_autocheckpoint` at SQLite's default of 1000 pages. That means
whichever write happens to fill the WAL inherits a page flush + fsync of the DB,
**while holding the caller's lock**, with every other request queued behind it.

| 430 KB body, commit + `INSERT`, copy of the live DB | p50 | max |
|---|---|---|
| `wal_autocheckpoint=1000` (default) | 2.19 ms | **114.8 ms** |
| `wal_autocheckpoint=0` | 1.82 ms | **4.7 ms** |
| …after the expired-row reclaim, default | 2.52 ms | 74.7 ms |
| …after the expired-row reclaim, `0` | 2.78 ms | 13.1 ms |

Fix: `PRAGMA wal_autocheckpoint=0`. Safe because the WAL is bounded elsewhere —
the daemon checkpoints every 60 s (`daemon._run_periodic_checkpoint`,
`truncate=True`) and on shutdown, and SQLite checkpoints when the last connection
closes, so short-lived CLI connections still leave no WAL behind.

## What this does NOT claim

- The exact mechanism of the single 32 s stall is **not fully proven**. Two
  measured drivers — a checkpoint folding into a write, and 1.1 GB of dead rows
  in the file being written — are both removed by A1/A2; whether that eliminates
  the stall is to be confirmed from the live trace after deployment, the same way
  the prune fix was verified.
- No token, billing or context claim. These are disk and latency changes only.
- The earlier `46.83 ms` vs `5.12 ms` commit A/B (from the floor analysis) is
  corroborated in direction, not in absolute value: re-measured here the p50 is
  ~2 ms in both arms and the difference shows in the **tail** (114.8 → 4.7 ms).
