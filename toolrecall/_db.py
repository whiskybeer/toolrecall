import os
import re
import sqlite3
import hashlib
import warnings
from threading import RLock
from contextlib import contextmanager
from toolrecall.config import load_config, Config

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
#   - When empty, it means "allow everything" (common in dev/single-user)
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
        db_path = os.path.expanduser(cfg.cache_db)
        # Detect DB path change (e.g. tests switching TOOLRECALL_CACHE_DB).
        # If the path changed, close the old connection AND reload config
        # so the new db path is picked up.
        if _db_real is not None:
            try:
                # sqlite3 exposes the DB path via .execute("PRAGMA database_list")
                if _db_path_cached != db_path:
                    _db_real.close()
                    _db_real = None
                    # Force config reload on DB path change — env var or
                    # test fixture may have changed TOOLRECALL_CACHE_DB.
                    global _cached_config
                    _cached_config = None
                    cfg = _get_cached_config()
                    db_path = os.path.expanduser(cfg.cache_db)
            except Exception:
                pass
        if _db_real is None:
            if cfg.storage_backend != "sqlite":
                warnings.warn(f"ToolRecall: Backend '{cfg.storage_backend}' not yet implemented. Falling back to 'sqlite'.")
            os.makedirs(os.path.dirname(db_path), exist_ok=True)
            _db_real = sqlite3.connect(db_path, timeout=30.0, check_same_thread=False)
            _db_real.execute("PRAGMA journal_mode=WAL;")
            _db_real.execute("PRAGMA synchronous=NORMAL;")
            _db_real.execute("PRAGMA busy_timeout=5000;")
            _db_real.row_factory = sqlite3.Row
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


def _init(schema: str = ""):
    """Initialize DB schema. Pass SCHEMA from cache.py."""
    with _db() as conn:
        if schema:
            conn.executescript(schema)
            conn.commit()
        # Migration: v0.3.x → v0.4.0 — rename tokens_intercepted to tokens_read_from_disk
        try:
            conn.execute("ALTER TABLE cache_stats RENAME COLUMN tokens_intercepted TO tokens_read_from_disk")
        except sqlite3.OperationalError:
            pass  # column already renamed (idempotent)
        # Migration: v0.7.5 → v0.8.0 — add tokens_saved column
        try:
            conn.execute("ALTER TABLE cache_stats ADD COLUMN tokens_saved INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            pass  # column already exists (idempotent)
        # Migration: v0.8.x → v0.9.0 — add updated_at column for cache_stats freshness
        try:
            conn.execute("ALTER TABLE cache_stats ADD COLUMN updated_at REAL DEFAULT 0")
        except sqlite3.OperationalError:
            pass  # column already exists (idempotent)
        # Migration: v0.9.4 → v0.10.0 — add context_tokens_saved column
        try:
            conn.execute("ALTER TABLE cache_stats ADD COLUMN context_tokens_saved INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            pass  # column already exists (idempotent)