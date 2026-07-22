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
