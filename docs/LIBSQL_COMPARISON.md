# ToolRecall Storage Backend Comparison

## `sqlite3` (stdlib) vs `libsql-experimental`

| Dimension | `sqlite3` (stdlib) | `libsql-experimental` |
|---|---|---|
| **Dependency size** | 0 (bundled with Python) | ~5MB wheel (Rust native) |
| **Install** | None (always available) | `pip install toolrecall[libsql]` |
| **RAM per connection** | ~2-5MB | ~10-20MB (Rust runtime) |
| **Writers** | Single writer (WAL allows concurrent reads) | `BEGIN CONCURRENT` — multi-writer |
| **Async I/O** | No | Linux `io_uring` support |
| **Vector search** | No | Native F32 vector type + similarity search |
| **Cloud sync** | No | Built-in embedded replica sync (push/pull) |
| **Scaling** | Single machine | Multi-machine via Turso Cloud sync |
| **Maturity** | 30+ years, battle-tested | Pre-1.0, rapid development |

## When to use which

### Use `sqlite3` (default) when:
- Single-machine/single-process usage
- Minimal dependency footprint is critical
- You want zero extra install steps
- Standard FTS5 full-text search is sufficient

### Use `libsql` when:
- Multiple agents or processes write concurrently
- You need semantic/vector cache lookup
- Cache is shared across machines via Turso Cloud
- Your workload benefits from async I/O

## Latency characteristics

Since sync is asynchronous background, local reads are always fast:

| Operation | Latency | Blocking? |
|---|---|---|
| Local cache read (hit) | ~0.6ms | No |
| Local cache read (miss → disk) | ~1.5s | Yes (normal) |
| Sync push (background) | ~50-500ms | No — async thread |
| Sync pull (background) | ~50-500ms | No — async thread |
| Remote query (future) | ~10-50ms | Opt-in only |

**Principle:** Local first, sync in background. Network never blocks a cache read.

## Configuration

Set the backend in `config.toml`:

```toml
[storage]
backend = "libsql"  # or "sqlite" (default)
# libsql_db = "~/.toolrecall/cache.db"

# Sync to Turso Cloud (optional)
# sync_url = "libs://my-db.turso.io"
# sync_token = "..."  # Turso auth token
# sync_interval = 60   # seconds between syncs, 0 = disabled
```

Or via environment variables:

```bash
export TOOLRECALL_STORAGE_BACKEND=libsql
export TOOLRECALL_LIBSQL_DB_PATH=~/.toolrecall/cache.db
export TOOLRECALL_SYNC_URL=libs://my-db.turso.io
export TOOLRECALL_SYNC_TOKEN=...
export TOOLRECALL_SYNC_INTERVAL=60
```

## Schema compatibility

Both backends share the same SQL schema. The `embedding BLOB` column on `file_cache` and `terminal_cache` is automatically added by the migration in `_init()` regardless of backend choice — it provides the schema foundation for vector search regardless of which backend is active.
