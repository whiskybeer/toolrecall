"""Storage backends for ToolRecall -- the swap point below the singleton.

Layering (see docs/ARCHITECTURE.md Section 5 and 5b):

    Bridges -> Daemon (LRU . singleton conn . IPC) -> storage backend -> disk

Everything above this package sees one sqlite3-compatible connection and
never imports a backend module directly. Everything backend-specific
lives in exactly one module here.

All backends beyond the stdlib default are OPTIONAL:

- `sqlite` -- stdlib, always available, the default. Zero extra deps.
- `libsql` -- optional extra (``pip install toolrecall[libsql]``).
  Importing this package NEVER requires the extra; the dependency is
  only touched when the backend is actually selected in config.
- `libsql-sync` -- optional extra (``pip install toolrecall[libsql-sync]``).
  Uses pyturso for Turso Cloud sync. Requires sync_url + sync_token.

Adding a backend = one new module exposing ``connect(cfg, db_path)``,
``SUPPORTS_SYNC`` + ``sync_configured(cfg)``, and one entry in
``_BACKENDS``. Nothing in daemon.py or cache.py changes.
"""

import os
import warnings

# Registry of known backends -> their module names under toolrecall.storage.
# Modules are imported lazily so optional deps are never touched unless
# the backend is selected.
_BACKENDS = {
    "sqlite": "toolrecall.storage.sqlite",
    "libsql": "toolrecall.storage.libsql",
    "libsql-sync": "toolrecall.storage.libsql_sync",
}

DEFAULT_BACKEND = "sqlite"


def _backend_module(name: str):
    """Import and return the backend module for name (lazy)."""
    import importlib
    return importlib.import_module(_BACKENDS[name])


def resolve_backend_name(cfg) -> str:
    """Return the effective backend name, warning + falling back on unknowns."""
    name = cfg.storage_backend
    if name not in _BACKENDS:
        warnings.warn(
            f"ToolRecall: Unknown storage_backend '{name}'. "
            f"Falling back to '{DEFAULT_BACKEND}'.")
        return DEFAULT_BACKEND
    return name


def active_db_path(cfg) -> str:
    """DB path for the ACTIVE backend (libsql variants use their own file)."""
    name = resolve_backend_name(cfg)
    if name in ("libsql", "libsql-sync"):
        return os.path.expanduser(cfg.libsql_db_path)
    return os.path.expanduser(cfg.cache_db)


def restrict_db_perms(db_path: str) -> None:
    """chmod 0600 the DB file (+WAL/SHM sidecars) -- the cache holds file contents."""
    for p in (db_path, db_path + "-wal", db_path + "-shm"):
        try:
            if os.path.exists(p):
                os.chmod(p, 0o600)
        except OSError:
            pass  # best-effort (e.g. foreign filesystem)


def open_backend(cfg):
    """Factory: return a sqlite3-compatible connection for the configured backend.

    This is the ONLY place a backend module is chosen. Callers (via
    toolrecall._db) receive an object honoring the sqlite3.Connection
    interface: execute/executemany/executescript, commit/rollback/close,
    rows subscriptable by index and column name.
    """
    name = resolve_backend_name(cfg)
    db_path = active_db_path(cfg)
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = _backend_module(name).connect(cfg, db_path)
    restrict_db_perms(db_path)
    return conn


def backend_supports_sync(cfg) -> bool:
    """Capability check: can the active backend sync at all?"""
    return bool(getattr(_backend_module(resolve_backend_name(cfg)), "SUPPORTS_SYNC", False))


def sync_configured(cfg) -> bool:
    """True iff background sync should run for the active backend.

    Encapsulates the FULL opt-in policy so daemon.py and _db.db_sync()
    share one source of truth and stay backend-agnostic:
      capability AND explicit enable (default off) AND credentials.
    """
    mod = _backend_module(resolve_backend_name(cfg))
    if not getattr(mod, "SUPPORTS_SYNC", False):
        return False
    check = getattr(mod, "sync_configured", None)
    return bool(check(cfg)) if check else False


def stats_info(cfg) -> dict:
    """Backend block for get_stats() -- keeps cache.py free of backend branches."""
    name = resolve_backend_name(cfg)
    mod = _backend_module(name)
    info = {"storage_backend": name, "sync_enabled": False,
            "sync_url": None, "sync_interval": None}
    fn = getattr(mod, "stats_info", None)
    if fn:
        info.update(fn(cfg))
    return info