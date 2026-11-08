# Hot-path DB work: access-log prune + off-path integrity check

Branch: `perf/access-log-prune`. Two changes, both about work the daemon did on
**every cache read**. Measured on the live daemon (1.12 GB `cache.db`), not in a
microbenchmark.

## 1. The access-log prune ran on every read (root cause of a 43 ms hit)

`_record()` — called on every cache read that has a path — ended with:

```sql
DELETE FROM access_log WHERE id NOT IN (
    SELECT id FROM access_log ORDER BY cached_at DESC LIMIT 50000)
```

`access_log` sits at its 50,000-row cap, so that subquery **sorted 50,000 rows
per read**:

| measurement | value |
|---|---|
| the subquery alone (`ORDER BY cached_at DESC LIMIT 50000`, live DB) | 45.3 ms |
| the full statement per read (copy of the live DB) | 86.4 ms |
| live daemon `cached_read` p50 over 32k samples | 43.30 ms |

Consequence: **a cache hit cost the same as a cold read** (~43 ms vs ~44 ms),
and `cached_read` was ≥30 ms on 87 % of calls. It was not Python, fsync, lock
contention or the pragmas — all four were measured and ruled out (see
`workspace/mcp-replay-bench/LOCAL_FLOOR_ANALYSIS.md`).

**Change:** prune at most once per `_ACCESS_PRUNE_INTERVAL` (60 s) and prune by
primary key:

```sql
DELETE FROM access_log WHERE id <=
    (SELECT coalesce(max(id), 0) FROM access_log) - ?
```

No sort, and off the hot path. `id` order == insertion order == `cached_at`
order, so the newest N rows are kept exactly as before.

| form | cost per read |
|---|---|
| before (per-read, `NOT IN` + `ORDER BY`) | 86.37 ms |
| after (PK form, gated to 1/60 s) | 0.09 ms |

Config: `[cache] access_log_keep` (default 50000),
`[cache] access_log_prune_interval` (default 60 s).

## 2. The integrity check froze the request loop (15–20 s every 10 min)

`_ensure_db_integrity()` ran `PRAGMA integrity_check` **inline**, on the caller's
thread and on the shared `_db()` connection, gated to once per 600 s:

- `PRAGMA integrity_check` on this DB: **15.6 s / 20.0 s** (two runs)
- the live log's `cached_read` max: **30,485 ms** — consistent with such a stall

**Change:** when due, start a daemon thread that runs the check on its **own
read-only connection** (`file:…?mode=ro`), guarded so only one check runs at a
time and skipped for non-sqlite backends. Consequences: the caller is never
blocked, the shared connection lock is never held for the check's duration (WAL
readers do not block writers), and recovery still triggers on corruption.

## Tests

`tests/test_access_log_prune.py` — the gate, the key-based form (asserts the
executed SQL contains `max(id)` and neither `ORDER BY` nor `NOT IN`), that the
newest rows survive, and a latency guard on a full 50k-row log (old behaviour
would blow the bound).

`tests/test_db_integrity_offpath.py` — caller is not blocked, `_db()` stays
available while a check is running, checks never stack, remote backends are
skipped, the check opens its own read-only connection, and a malformed DB still
triggers recovery.

## Not in this branch

Pragma changes (`synchronous`, `wal_autocheckpoint`). The daemon already runs
`synchronous=NORMAL` (`toolrecall/storage/sqlite.py:16`); an earlier
investigation blamed `synchronous=FULL` and was corrected — see
`LOCAL_FLOOR_ANALYSIS.md` §"Correction to my earlier attribution".

## Verification after merge

```bash
toolrecall cache-status                     # sanity
tail -50 ~/.toolrecall/logs/toolrecall.log | grep cached_read
```

Expect `cached_read` to drop from ~43 ms to sub-millisecond levels, with the
10-minute multi-second stall gone.
