"""Optional storage backend: libSQL sync via pyturso.

OPTIONAL in every sense:
- Selected only via [storage].backend = "libsql-sync" (default is sqlite)
- Dependency installed only via pip install toolrecall[libsql-sync]
- This module always imports cleanly WITHOUT the dependency -- only
  connect() raises, with an install hint, if it's missing
- Sync to Turso Cloud is handled via turso.sync.connect() using the
  same API documented at https://docs.turso.tech/sdk/python
- NOTE: local sqld (libsql-server) is NOT yet supported. The pyturso
  sync protocol version must match the server. For local-only libSQL
  use backend="libsql" (libsql-experimental).

Uses the same wrapper classes from storage.libsql so call sites never
know which libSQL variant they got.
"""

from toolrecall.storage.libsql import _LibSQLConnection

try:
    import turso.sync as turso_sync
    _HAS_PYTURSO = True
except ImportError:
    turso_sync = None
    _HAS_PYTURSO = False

SUPPORTS_SYNC = True


def sync_configured(cfg) -> bool:
    """Full opt-in policy: explicit enable (default false) AND credentials."""
    return bool(
        cfg.libsql_sync_enabled
        and cfg.libsql_sync_url
        and cfg.libsql_sync_token
    )


def stats_info(cfg) -> dict:
    """Backend block for get_stats()."""
    return {
        "sync_enabled": cfg.libsql_sync_enabled,
        "sync_url": cfg.libsql_sync_url or "not configured",
        "sync_interval": cfg.libsql_sync_interval,
        "sync_backend": "pyturso",
    }


def connect(cfg, db_path: str):
    """Open a pyturso sync connection (embedded replica)."""
    if not _HAS_PYTURSO:
        raise ImportError(
            "storage_backend='libsql-sync' requires pyturso.\n"
            "  Install: pip install toolrecall[libsql-sync]"
        )
    if not sync_configured(cfg):
        raise ValueError(
            "storage_backend='libsql-sync' requires sync_url and sync_token.\n"
            "  Set TOOLRECALL_SYNC_ENABLED=true, TOOLRECALL_SYNC_URL, and\n"
            "  TOOLRECALL_SYNC_TOKEN. For local-only libSQL, use backend='libsql' instead."
        )
    conn = turso_sync.connect(
        db_path,
        remote_url=cfg.libsql_sync_url,
        auth_token=cfg.libsql_sync_token,
    )
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA busy_timeout=5000;")
    return _LibSQLConnection(conn)
