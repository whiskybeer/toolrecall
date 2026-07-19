import os
import re
import sqlite3
import hashlib
import warnings
from threading import RLock
from contextlib import contextmanager
from toolrecall.config import load_config, Config

from toolrecall import storage

config = load_config()


# Module-level config cache for _db() — avoids re-reading TOML from disk
# on every single SQLite entry (38 call sites).  Reloaded only when the
# DB path changes (detected via _db_path_cached in _db()).
_cached_config: Config | None = None


def _get_cached_config() -> Config:
    """Return the cached Config, re-loading only on demand.

    Unlike the hot-path load_config() call that was previously inside _db(),
    this caches the Config instance and only re-reads TOML when the caller
    explicitly requests a reload (e.g. when the DB path changed).
    """
    global _cached_config
    if _cached_config is None:
        _cached_config = load_config()
    return _cached_config


# ─── Hash Helper (pluggable algorithm) ─────────────────────
# Controlled by [cache].hash_algorithm in config.toml.
# "md5" (default) — fast, backward-compatible, fine for cache keying.
# "sha256" — slightly slower, better cryptographic hygiene.
_HASH_ALGORITHM = str(config.get("cache", "hash_algorithm", default="md5") or "md5").lower()


def _hash(value: str) -> str:
    """Hash a string for cache keying using the configured algorithm.
    Algorithm is set via [cache].hash_algorithm in config.toml.
    MD5 is the default — fast, non-cryptographic here, keeps cache keys valid.
    SHA256 is available for stricter environments.
    """
    if _HASH_ALGORITHM == "sha256":
        return hashlib.sha256(value.encode()).hexdigest()
    return hashlib.md5(value.encode()).hexdigest()


# ─── Sensitive File Blocklist ───────────────────────────────
#
# These patterns block file paths from being cached or read through
# the ToolRecall cache layer.  The blocklist applies at EVERY entry
# point: cached_read() (direct), SecurityGate.check_read_path() (daemon),
# and cache invalidations.
#
# Why not just rely on allowed_paths?
#   - allowed_paths is an allowlist — it requires explicit configuration
#   - When empty, it means NO paths are readable (default-deny)
#   - A blocklist catches the obvious sensitive files EVEN when the
#     user hasn't configured an allowlist yet
#   - Both mechanisms compose: allowlist narrows scope FIRST,
#     blocklist catches slips SECOND
#
# Each pattern is compiled lazily on first use.  A path matching ANY
# entry is rejected from caching/reading.

# Compiled lazily on first call to _is_sensitive_path()
_SENSITIVE_PATTERNS = None

# Pattern definitions (raw strings — compiled on first use)
SENSITIVE_FILE_PATTERNS = [
    # Shell configs / credentials
    r"(^|/)\.bashrc($|/)",                        # .bashrc
    r"(^|/)\.zshrc($|/)",
    r"(^|/)\.profile($|/)",                       # .profile
    r"(^|/)\.env(\.[a-zA-Z]+)?$",                 # .env, .env.local, .env.production
    r"(^|/)\.gitconfig$",                         # git config (may hold tokens)
    r"(^|/)\.netrc$",                             # machine credentials
    r"(^|/)\.npmrc$",                             # npm registry tokens
    r"(^|/)\.dockercfg$",                         # Docker registry auth
    r"(^|/)\.docker/config\.json$",               # Docker config (may hold creds)

    # SSH
    r"(^|/)\.ssh/",                               # SSH keys, config, authorized_keys

    # Token / key files
    r"(^|/)\.token$",                             # Generic token file
    r"(^|/)\.secret$",                            # Generic secret file
    r"(^|/)credentials\.json$",                   # GCP / service account keys
    r"(^|/)credentials\.ini$",                    # AWS / generic
    r"(^|/)(id_rsa|id_ecdsa|id_ed25519)($|/|\.)",  # SSH private keys by name

    # Common config dirs that hold secrets
    r"(^|/)\.config/gcloud/",                     # GCP service account keys
    r"(^|/)\.config/gh/",                         # GitHub CLI tokens
    r"(^|/)\.aws/",                               # AWS credentials + config
    r"(^|/)\.azure/",                             # Azure CLI credentials

    # Session/cookies
    r"(^|/)cookie\.txt$",                         # session cookies

    # ToolRecall's OWN secrets — the config may contain the Turso sync
    # token, and the cache DBs contain everything ever cached. Reading
    # them back through the cache layer would let an agent exfiltrate
    # both. (The daemon accesses the DB directly, not via cached_read,
    # so this does not break normal operation.)
    r"(^|/)\.config/toolrecall/",                 # toolrecall.toml (sync token), .env
    r"(^|/)\.toolrecall/.*\.db(-wal|-shm)?$",     # cache.db, cache-libsql.db + sidecars
]

# Sensitive file extensions (checked on splitext basename)
SENSITIVE_FILE_EXTENSIONS = {".pem", ".key", ".cert", ".p12", ".pfx"}

# Sensitive basenames — checked on os.path.basename only
SENSITIVE_BASENAMES = frozenset({
    ".env", ".token", ".secret",
    "credentials.json", "credentials.ini",
    ".netrc", ".gitconfig", ".npmrc", ".dockercfg",
})


def _compile_sensitive_patterns():
    """Compile regex patterns lazily (avoids import-time cost)."""
    global _SENSITIVE_PATTERNS
    if _SENSITIVE_PATTERNS is None:
        _SENSITIVE_PATTERNS = [re.compile(p) for p in SENSITIVE_FILE_PATTERNS]
    return _SENSITIVE_PATTERNS


def _is_sensitive_path(path: str) -> bool:
    """Check if a file path matches any sensitive-file pattern.

    Returns True if the path SHOULD BE BLOCKED from caching/reading.
    Used by cached_read() and SecurityGate.check_read_path().

    Note: This is a path-name check, not a content scan.  Renaming a
    sensitive file to 'my-config.txt' bypasses it — but that's an
    intentional choice by the user (they removed the protection by
    moving the file). For stronger guarantees, combine with
    allowed_paths allowlisting.
    """
    import os as _os
    # Normalize: expand ~, resolve symlinks, collapse /./ and ///
    expanded = _os.path.realpath(_os.path.expanduser(path))

    # Check regex patterns
    patterns = _compile_sensitive_patterns()
    for pat in patterns:
        if pat.search(expanded):
            import logging
            logging.getLogger("toolrecall.db").warning(
                "Blocked read of sensitive file: %s (matched pattern: %s)", path, pat.pattern)
            return True

    # Check extension on basename
    import os.path as _osp
    base, ext = _osp.splitext(_osp.basename(expanded))
    if ext and ext.lower() in SENSITIVE_FILE_EXTENSIONS:
        import logging
        logging.getLogger("toolrecall.db").warning(
            "Blocked read of sensitive file: %s (matched extension: %s)", path, ext)
        return True

    # Check basename
    if _osp.basename(expanded) in SENSITIVE_BASENAMES:
        import logging
        logging.getLogger("toolrecall.db").warning(
            "Blocked read of sensitive file: %s (matched basename)", path)
        return True

    return False


# ─── DB Connection Factory (delegates to toolrecall.storage) ──────
#
# All backend-specific code lives in toolrecall/storage/ (see
# docs/ARCHITECTURE.md §5b). This module keeps ONLY what the docs say
# it owns: the singleton connection, the RLock, and the blocklist.
# The names below are re-exported for backward compatibility — tests
# and older call sites import them from toolrecall._db.

from toolrecall.storage import (          # noqa: E402
    active_db_path as _active_db_path,
    open_backend as _open_db_backend,
    restrict_db_perms as _restrict_db_perms,
)
from toolrecall.storage.libsql import (   # noqa: E402  (import-safe w/o extra)
    _LibSQLConnection,
    _LibSQLCursor,
    _LibSQLRow,
    _LibSQLRowIter,
)


def _open_db(cfg):
    """Factory: return a connection for the configured backend.

    Kept as a thin shim over toolrecall.storage.open_backend() for
    backward compatibility (tests and external callers import it here).
    """
    return _open_db_backend(cfg)


# ─── Singleton SQLite Connection (thread-safe, context-managed) ────

_db_lock = RLock()
_db_real: sqlite3.Connection | None = None
_db_refcount: int = 0  # Reentrancy counter: commit only at outermost exit
_db_path_cached: str | None = None  # Track DB path to detect env var changes


@contextmanager
def _db():
    """Context manager: acquire DB lock, yield singleton connection, commit+release on exit.

    Thread-safe via RLock. Reentrant-safe via refcount — inner nested calls
    do NOT prematurely commit the outer transaction.

    Bug fixes applied:
    - If connection init fails, _db_real stays None; finally guards against
      calling .commit() on None (previously crashed with AttributeError,
      leaving refcount at -1 and breaking all future commits).
    - Commit only fires on the success path (else clause). Previously the
      finally block always committed when refcount hit 0, even after a
      rollback in the except branch — committing rolled-back state.
    - Removed the second yield inside except. A context manager body can
      only be entered once; the retry yield was a no-op that didn't re-run
      the caller's queries. Callers must retry by re-entering the context.

    Usage:
        with _db() as conn:
            conn.execute("INSERT ...")
            row = conn.execute("SELECT ...").fetchone()
        # auto-commits and releases lock

    On exception: rollback + release (no lock leak).
    """
    global _db_real, _db_refcount, _db_path_cached
    _db_lock.acquire()
    _should_commit = False  # Initialize before try — always in scope
    try:
        cfg = _get_cached_config()
        db_path = _active_db_path(cfg)
        # Detect DB path change (e.g. tests switching TOOLRECALL_CACHE_DB or
        # TOOLRECALL_LIBSQL_DB_PATH). If the path changed, close the old
        # connection AND reload config so the new db path is picked up.
        # _active_db_path() tracks the path of the ACTIVE backend, so
        # switching backends also triggers a clean reconnect.
        if _db_real is not None:
            try:
                if _db_path_cached != db_path:
                    _db_real.close()
                    _db_real = None
                    # Force config reload on DB path change — env var or
                    # test fixture may have changed the path or backend.
                    global _cached_config
                    _cached_config = None
                    cfg = _get_cached_config()
                    db_path = _active_db_path(cfg)
            except Exception:
                pass
        if _db_real is None:
            # Unknown-backend warning + fallback handled in storage.resolve_backend_name()
            _db_real = _open_db(cfg)
            _db_path_cached = db_path
        _db_refcount += 1
        try:
            yield _db_real
        except sqlite3.OperationalError as _db_err:
            _db_real.rollback()
            raise
        except Exception:
            _db_real.rollback()
            raise
        else:
            _should_commit = True
    finally:
        # Only decrement if we successfully incremented
        if _db_refcount > 0:
            _db_refcount -= 1
        # Commit only when outermost context exits, body succeeded,
        # and connection exists
        if _db_refcount == 0 and _db_real is not None and _should_commit:
            _db_real.commit()
        _db_lock.release()


def db_sync() -> bool:
    """Sync the shared libSQL embedded replica with Turso Cloud.

    Preconditions (checked via storage.sync_configured() — callers can
    invoke unconditionally):
      - the active backend supports sync at all
      - sync is explicitly enabled (opt-in, default False)
      - credentials are configured

    Runs against the SAME singleton connection used for all reads/writes,
    under _db_lock — no second file handle, no WAL contention with the
    daemon's own writes.

    Returns True if a sync was performed, False if skipped (not configured).
    Raises on actual sync errors so the caller can log/backoff.
    """
    cfg = _get_cached_config()
    if not storage.sync_configured(cfg):
        return False
    with _db() as conn:
        conn.sync()
    return True


def _init(schema: str = ""):
    """Initialize DB schema. Pass SCHEMA from cache.py."""
    with _db() as conn:
        if schema:
            conn.executescript(schema)
            conn.commit()
        # Migration: v0.3.x → v0.4.0 — rename tokens_intercepted to tokens_read_from_disk
        try:
            conn.execute("ALTER TABLE cache_stats RENAME COLUMN tokens_intercepted TO tokens_read_from_disk")
        except (sqlite3.OperationalError, ValueError, Exception):
            pass  # column already renamed (idempotent)
        # Migration: v0.7.5 → v0.8.0 — add tokens_saved column
        try:
            conn.execute("ALTER TABLE cache_stats ADD COLUMN tokens_saved INTEGER DEFAULT 0")
        except (sqlite3.OperationalError, ValueError, Exception):
            pass  # column already exists (idempotent)
        # Migration: v0.8.x → v0.9.0 — add updated_at column for cache_stats freshness
        try:
            conn.execute("ALTER TABLE cache_stats ADD COLUMN updated_at REAL DEFAULT 0")
        except (sqlite3.OperationalError, ValueError, Exception):
            pass  # column already exists (idempotent)
        # Migration: v0.9.4 → v0.10.0 — add context_tokens_saved column
        try:
            conn.execute("ALTER TABLE cache_stats ADD COLUMN context_tokens_saved INTEGER DEFAULT 0")
        except (sqlite3.OperationalError, ValueError, Exception):
            pass  # column already exists (idempotent)
        # Migration: v0.x → v0.y — add stderr column to terminal_cache
        try:
            conn.execute("ALTER TABLE terminal_cache ADD COLUMN stderr TEXT NOT NULL DEFAULT ''")
        except (sqlite3.OperationalError, ValueError, Exception):
            pass  # column already exists (idempotent)
        # Migration: v0.10.x → v0.11.0 — add embedding column for vector search (libSQL)
        try:
            conn.execute("ALTER TABLE file_cache ADD COLUMN embedding BLOB")
        except (sqlite3.OperationalError, ValueError, Exception):
            pass  # column already exists (idempotent)
        try:
            conn.execute("ALTER TABLE terminal_cache ADD COLUMN embedding BLOB")
        except (sqlite3.OperationalError, ValueError, Exception):
            pass  # column already exists (idempotent)