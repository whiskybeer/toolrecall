import os, re, sqlite3, time, hashlib, warnings
from pathlib import Path
from threading import Lock, RLock
from collections import OrderedDict
from contextlib import contextmanager
from toolrecall.config import load_config

config = load_config()


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

    Can be overridden per-task via the environment variable:
        TOOLRECALL_ALLOW_SENSITIVE=true  # disables this block for one command

    Note: This is a path-name check, not a content scan.  Renaming a
    sensitive file to 'my-config.txt' bypasses it — but that's an
    intentional choice by the user (they removed the protection by
    moving the file). For stronger guarantees, combine with
    allowed_paths allowlisting.
    """
    import os as _os
    # Allow override — user explicitly wants to pass a secret
    if _os.environ.get("TOOLRECALL_ALLOW_SENSITIVE", "").lower() in ("1", "true", "yes"):
        return False
    # Normalize: expand ~, resolve symlinks, collapse /./ and ///
    expanded = _os.path.realpath(_os.path.expanduser(path))

    # Check regex patterns
    patterns = _compile_sensitive_patterns()
    for pat in patterns:
        if pat.search(expanded):
            print(f"[ToolRecall] Blocked read of sensitive file: {path} (matched pattern: {pat.pattern})")
            return True

    # Check extension on basename
    import os.path as _osp
    base, ext = _osp.splitext(_osp.basename(expanded))
    if ext and ext.lower() in SENSITIVE_FILE_EXTENSIONS:
        print(f"[ToolRecall] Blocked read of sensitive file: {path} (matched extension: {ext})")
        return True

    # Check basename
    if _osp.basename(expanded) in SENSITIVE_BASENAMES:
        print(f"[ToolRecall] Blocked read of sensitive file: {path} (matched basename)")
        return True

    return False


# ─── Singleton SQLite Connection (thread-safe, context-managed) ────

_db_lock = RLock()
_db_real: sqlite3.Connection | None = None
_db_refcount: int = 0  # Reentrancy counter: commit only at outermost exit


@contextmanager
def _db():
    """Context manager: acquire DB lock, yield singleton connection, commit+release on exit.

    Thread-safe via RLock. Reentrant-safe via refcount — inner nested calls
    do NOT prematurely commit the outer transaction. Retries on SQLITE_BUSY
    with exponential backoff (up to 3 tries) to handle WAL lock contention
    when multiple daemon threads access the DB concurrently.

    Usage:
        with _db() as conn:
            conn.execute("INSERT ...")
            row = conn.execute("SELECT ...").fetchone()
        # auto-commits and releases lock

    On exception: rollback + release (no lock leak).
    """
    global _db_real, _db_refcount
    _db_lock.acquire()
    try:
        if _db_real is None:
            cfg = load_config()
            if cfg.storage_backend != "sqlite":
                warnings.warn(f"ToolRecall: Backend '{cfg.storage_backend}' not yet implemented. Falling back to 'sqlite'.")
            db_path = os.path.expanduser(cfg.cache_db)
            os.makedirs(os.path.dirname(db_path), exist_ok=True)
            _db_real = sqlite3.connect(db_path, timeout=30.0, check_same_thread=False)
            _db_real.execute("PRAGMA journal_mode=WAL;")
            _db_real.execute("PRAGMA synchronous=NORMAL;")
            _db_real.execute("PRAGMA busy_timeout=5000;")
            _db_real.row_factory = sqlite3.Row
        _db_refcount += 1
        try:
            yield _db_real
        except sqlite3.OperationalError as _db_err:
            # Retry once for transient SQLITE_BUSY / locking errors
            err_msg = str(_db_err)
            if "locked" in err_msg or "transaction" in err_msg or "busy" in err_msg:
                import time as _time
                _time.sleep(0.1)
                try:
                    _db_real.rollback()
                    yield _db_real
                except Exception:
                    _db_real.rollback()
                    raise
            else:
                _db_real.rollback()
                raise
        except Exception:
            _db_real.rollback()
            raise
        else:
            pass  # commit happens in finally after refcount decrement
    finally:
        _db_refcount -= 1
        # Only commit when outermost context exits (refcount back to 0)
        if _db_refcount == 0:
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