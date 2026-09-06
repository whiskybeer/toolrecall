# ToolRecall Storage Backends

This package provides pluggable storage backends for the ToolRecall daemon. Everything above this layer sees a single `sqlite3`-compatible connection and never imports a backend module directly.

## Architecture

```
Bridges → Daemon (LRU · singleton conn · IPC) → storage backend → disk
```

The backend is the swap point below the singleton — swap the backend in config without touching any cache or daemon code.

## Backends

| Module | Backend | Deps | Sync |
|--------|---------|------|------|
| [`sqlite.py`](./sqlite.py) | CPython `sqlite3` (stdlib) | None | No |
| [`libsql.py`](./libsql.py) | libSQL (via `libsql-experimental`) | `pip install toolrecall[libsql]` | No |
| [`libsql_sync.py`](./libsql_sync.py) | libSQL + Turso Cloud sync | `pip install toolrecall[libsql-sync]` | Yes (via pyturso) |

The default backend is `sqlite` — zero extra dependencies.

All optional-extras backends are **lazy-imported**: the dependency is only touched when that backend is actually selected in config.

## Adding a Backend

1. Create a new module exposing:
   - `connect(cfg, db_path)` → a `sqlite3.Connection`-compatible object
   - `SUPPORTS_SYNC` — `True`/`False`
   - `sync_configured(cfg)` (if sync-capable)
   - `stats_info(cfg)` (optional, for `get_stats()`)
2. Add an entry in the `_BACKENDS` registry in [`__init__.py`](./__init__.py)
3. Add the optional dependency to `pyproject.toml`

## Why These Backends (and Not Others)

The backend contract is narrow by design: everything above the singleton speaks
SQLite dialect and assumes a `sqlite3.Connection`-shaped object (rows
subscriptable by index *and* column name, `INSERT OR REPLACE`, `PRAGMA`
maintenance, FTS5 in `docs.py`). That constraint is what keeps daemon.py,
cache.py, and docs.py backend-agnostic — and it is the yardstick for every
"should we add backend X?" question.

### Why not Redis

- **Wrong data model.** Redis is a key-value store, not relational. The ~30 SQL
  call sites in cache.py and _db.py issue real queries (filters, upserts,
  multi-column rows) — each would need a hand-written reimplementation, and the
  sqlite3-connection contract would be abandoned entirely.
- **No feature gap to fill.** The things Redis is usually pulled in for already
  exist: TTLs are `expires_at` columns enforced by the daemon, and the
  LRU/ephemeral hot layer is `mcp_cache_fs`. Redis would add a second source of
  truth plus a network dependency for no new capability.
- **Violates the identity.** A Redis client in core breaks
  zero-runtime-deps (`dependencies=[]`); as an optional extra it still requires
  a running Redis server, which no other optional backend demands.

### Why not PostgreSQL (yet)

- **It's a dialect port, not a backend module.** The `connect()` shim is easy;
  the cost is above the singleton: `INSERT OR REPLACE` → `ON CONFLICT(pk) DO
  UPDATE`, `AUTOINCREMENT` → `BIGSERIAL`, `PRAGMA table_info` →
  `information_schema`, `wal_checkpoint` → (nothing), and a full rewrite of
  docs.py's FTS5 (virtual tables, sync triggers, BM25 ranking, the
  index-compact self-heal) onto tsvector/GIN — which ranks differently than
  BM25. Realistic effort: 1.5–2 weeks plus permanent dual-dialect maintenance.
- **No current requirement.** PostgreSQL earns its cost with many concurrent
  writers or multi-service shared access. ToolRecall's workload is one daemon
  process with a singleton connection — SQLite WAL already covers it.
- **If the need appears, reach for remote libSQL first** (see below): it
  provides the network/shared-database property at ~zero dialect cost.

### Why Turso / libSQL is the network alternative

libSQL **is** SQLite — same dialect, same FTS5. So the `libsql` backend keeps
every one of the 38+ call sites working unchanged while changing only where the
bytes live:

- `backend = "libsql"` → local file via `libsql-experimental` (drop-in).
- `libsql-sync` + credentials → embedded replica with Turso Cloud sync.
- Self-hosted: the same backend works against a local `sqld` container
  (docker-compose) — network/shared access without Turso Cloud or any new
  code.

That is the 10%-effort version of what PostgreSQL provides, and it is why
PostgreSQL stays out until a concrete multi-writer requirement exists.

### Other databases considered

| Candidate | Verdict | Reason |
|-----------|---------|--------|
| **DuckDB** | No | Analytical (OLAP) engine; wrong fit for point-read/write cache workload, and its Python API is not sqlite3-shaped. |
| **LMDB / RocksDB** | No | Key-value like Redis — same contract break, fewer features than SQLite for this schema. |
| **rqlite / dqlite** (distributed SQLite) | Watch | SQLite semantics over Raft — dialect-compatible in principle, but no driver presents a `sqlite3.Connection`-shaped API today (rqlite is HTTP/JSON). Revisit if multi-node SQLite is ever needed; a remote-`sqld` backend module would be the same shape. |
| **Cloudflare D1** | Watch | SQLite over HTTP — dialect-compatible, but needs an HTTP-adapter backend module and ties the daemon to a cloud account; same module shape as a future remote-sqld backend. |

### Decision rule for future backends

Accept a new backend only if **all** of these hold:

1. It speaks SQLite dialect (or a thin adapter can present sqlite3 semantics).
2. It fills a gap SQLite/local-libSQL does not (true concurrent multi-writer,
   cross-machine sharing).
3. It ships as a pip extra, lazy-imported — core stays `dependencies=[]`.

Anything key-value-shaped (Redis, LMDB, RocksDB) fails rule 1 and duplicates
existing TTL/LRU machinery; anything relational-but-different-dialect
(PostgreSQL, MySQL) fails on maintenance cost until rule 2 is real.
