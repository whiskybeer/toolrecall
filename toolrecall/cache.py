"""
ToolRecall Cache Core — Hybrid In-Memory + SQLite cache.

Architecture:
  ┌──────────────────────────────────────┐
  │  In-Memory Dict (file_cache)         │  ← ~0.001ms lookup
  │  path → {content, mtime, size}       │
  │  LRU eviction @ max_memory_mb        │
  │                                     │
  │  ▸ cached_read() checks here 1st     │
  │  ▸ auto-invalidates on mtime         │
  │  ▸ survives the agent session        │
  └────────────────┬─────────────────────┘
                   │ on miss / write-back
                   ▼
  ┌──────────────────────────────────────┐
  │  Singleton SQLite (with _db())       │  ← context manager, RLock-guarded
  │  Persists across sessions            │
  │  Serves HTTP proxy mode              │
  │  Auto commit/release on exit        │
  └──────────────────────────────────────┘

Secondary caches (terminal, script, code) use SQLite directly.

Token estimation: len(content) // 3  →  approximates typical LLM tokenizer
(English ~4 chars/token, code ~2 char/token → weighted average ~3)
"""
import os
import time
import warnings
from datetime import datetime
from threading import Lock
from collections import OrderedDict
from toolrecall.config import load_config
from toolrecall._db import _db
from toolrecall._db import _init as _db_init
from toolrecall._db import _hash

# Re-export for test compatibility: toolrecall.cache._init() calls _db_init with SCHEMA
def _init():
    _db_init(schema=SCHEMA)

config = load_config()


# _hash, SENSITIVE_FILE_PATTERNS, SENSITIVE_FILE_EXTENSIONS, SENSITIVE_BASENAMES,
# _compile_sensitive_patterns, _is_sensitive_path are imported from toolrecall._db.
# We re-import _is_sensitive_path explicitly below for use within this module.
from toolrecall._db import _is_sensitive_path as _is_sensitive_path
from toolrecall._db import _compile_sensitive_patterns as _compile_sensitive_patterns
from toolrecall.normalizer import normalize_tool_args, normalize_command

# ─── In-memory file cache with LRU ──────────────────────────

MAX_MEMORY_MB = config.get("cache", "max_memory_mb", default=20)  # Default 20 MB (∼model context window)
MAX_MEMORY_BYTES = MAX_MEMORY_MB * 1024 * 1024

class LRUCache:
    """Thread-safe LRU dict with byte-size tracking."""
    def __init__(self, max_bytes: int):
        self._data: OrderedDict[str, dict] = OrderedDict()
        self._lock = Lock()
        self._current_bytes = 0
        self._max_bytes = max_bytes

    def get(self, key: str):
        with self._lock:
            if key not in self._data:
                return None
            self._data.move_to_end(key)  # LRU: mark as recently used
            return self._data[key]

    def put(self, key: str, value: dict):
        size = len(value.get("content", ""))
        with self._lock:
            # If key exists, subtract old size first
            if key in self._data:
                old = self._data[key]
                self._current_bytes -= len(old.get("content", ""))
            # Evict until under limit
            while self._current_bytes + size > self._max_bytes and len(self._data) > 0:
                oldest_key, oldest_val = self._data.popitem(last=False)
                self._current_bytes -= len(oldest_val.get("content", ""))
            self._data[key] = value
            self._data.move_to_end(key)
            self._current_bytes += size

    def remove(self, key: str):
        with self._lock:
            if key in self._data:
                self._current_bytes -= len(self._data[key].get("content", ""))
                del self._data[key]

    def clear(self):
        with self._lock:
            self._data.clear()
            self._current_bytes = 0

    def __len__(self):
        with self._lock:
            return len(self._data)

    def memory_bytes(self):
        with self._lock:
            return self._current_bytes


_file_cache: LRUCache = LRUCache(MAX_MEMORY_BYTES)

# ─── SQLite Schema ──────────────────────────────────────────

SCHEMA = """
CREATE TABLE IF NOT EXISTS file_cache (
    path_hash TEXT PRIMARY KEY,
    path TEXT NOT NULL,
    content TEXT NOT NULL,
    mtime REAL NOT NULL,
    size INTEGER NOT NULL,
    cached_at REAL NOT NULL,
    hits INTEGER DEFAULT 1
);
CREATE TABLE IF NOT EXISTS skill_cache (
    skill_name TEXT PRIMARY KEY,
    content TEXT NOT NULL,
    file_count INTEGER DEFAULT 0,
    cached_at REAL NOT NULL,
    hits INTEGER DEFAULT 1
);
CREATE TABLE IF NOT EXISTS terminal_cache (
    command_hash TEXT PRIMARY KEY,
    command TEXT NOT NULL,
    output TEXT NOT NULL,
    exit_code INTEGER NOT NULL,
    cached_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    hits INTEGER DEFAULT 1
);
CREATE TABLE IF NOT EXISTS script_cache (
    script_hash TEXT PRIMARY KEY,
    script_path TEXT NOT NULL,
    args TEXT,
    output TEXT NOT NULL,
    exit_code INTEGER NOT NULL,
    cached_at REAL NOT NULL,
    hits INTEGER DEFAULT 1
);
CREATE TABLE IF NOT EXISTS code_cache (
    code_hash TEXT PRIMARY KEY,
    code TEXT NOT NULL,
    output TEXT NOT NULL,
    exit_code INTEGER NOT NULL,
    cached_at REAL NOT NULL,
    hits INTEGER DEFAULT 1
);
CREATE TABLE IF NOT EXISTS cache_stats (
    category TEXT PRIMARY KEY,
    hits INTEGER DEFAULT 0,
    misses INTEGER DEFAULT 0,
    tokens_read_from_disk INTEGER DEFAULT 0,
    tokens_saved INTEGER DEFAULT 0,
    updated_at REAL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS access_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    category TEXT NOT NULL,
    path TEXT,
    hit INTEGER NOT NULL,
    tokens INTEGER DEFAULT 0,
    cached_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_access_log_time ON access_log(cached_at DESC);
CREATE INDEX IF NOT EXISTS idx_file_mtime ON file_cache(mtime);
CREATE INDEX IF NOT EXISTS idx_terminal_expires ON terminal_cache(expires_at);
CREATE INDEX IF NOT EXISTS idx_file_path ON file_cache(path);
CREATE TABLE IF NOT EXISTS mcp_cache (
    request_hash TEXT PRIMARY KEY,
    mcp_server TEXT NOT NULL,
    mcp_tool TEXT NOT NULL,
    arguments TEXT,
    data TEXT NOT NULL,
    cached_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    hits INTEGER DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_mcp_expires ON mcp_cache(expires_at);
CREATE TABLE IF NOT EXISTS browser_cache (
    cache_key TEXT PRIMARY KEY,
    url TEXT NOT NULL,
    content_type TEXT NOT NULL DEFAULT 'snapshot',
    content TEXT NOT NULL,
    title TEXT,
    content_hash TEXT,
    cached_at REAL NOT NULL,
    hits INTEGER DEFAULT 1
);
CREATE TABLE IF NOT EXISTS api_cache (
request_hash TEXT PRIMARY KEY,
method TEXT NOT NULL DEFAULT 'POST',
host TEXT NOT NULL,
path TEXT NOT NULL,
request_body_hash TEXT NOT NULL,
request_body_preview TEXT,
response_status INTEGER,
response_headers TEXT,
response_body TEXT NOT NULL,
cached_at REAL NOT NULL,
expires_at REAL NOT NULL,
hits INTEGER DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_api_expires ON api_cache(expires_at);
"""


def _estimate_tokens(text: str) -> int:
    """Estimate LLM token count: code-heavy ~2 char/token, English ~4.
    Weighted average ~3 char/token — better than len//4 for agent content.
    """
    if not text:
        return 0
    return max(1, len(text) // 3)


def _record(category, hit: bool, tokens_read: int = 0, path: str = "", tokens_saved: int = 0):
    """Track cache statistics + log access.

    - hit=True:  increments hit counter and tokens_saved
    - hit=False: increments miss counter
    - tokens_read: (only on disk-read miss) records how many tokens
      were read from disk. NOT set on cache hits.
    - path: optional file path for access logging
    - tokens_saved: (only on cache hit) records how many tokens were
      served from cache without disk I/O
    """
    try:
        with _db() as conn:
            if hit:
                conn.execute("""
                    INSERT INTO cache_stats (category, hits, misses, tokens_read_from_disk, tokens_saved, updated_at)
                    VALUES (?, 1, 0, 0, 0, ?)
                    ON CONFLICT(category) DO UPDATE SET
                        hits = hits + 1,
                        tokens_saved = tokens_saved + ?,
                        updated_at = ?
                """, (category, time.time(), tokens_saved, time.time()))
            else:
                conn.execute("""
                    INSERT INTO cache_stats (category, hits, misses, tokens_read_from_disk, updated_at)
                    VALUES (?, 0, 1, 0, ?)
                    ON CONFLICT(category) DO UPDATE SET
                        misses = misses + 1,
                        updated_at = ?
                """, (category, time.time(), time.time()))
            if tokens_read:
                conn.execute("""
                    INSERT INTO cache_stats (category, hits, misses, tokens_read_from_disk, updated_at)
                    VALUES (?, 0, 0, ?, ?)
                    ON CONFLICT(category) DO UPDATE SET
                        tokens_read_from_disk = tokens_read_from_disk + ?,
                        updated_at = ?
                """, (category, tokens_read, time.time(), tokens_read, time.time()))
            # Access log — only record entries with a meaningful path
            # (skip noise like terminal, mcp, api, browser cache hits without paths)
            if path:
                conn.execute("""
                    INSERT INTO access_log (category, path, hit, tokens, cached_at)
                    VALUES (?, ?, ?, ?, ?)
                """, (category, path, 1 if hit else 0, tokens_saved if hit else tokens_read, time.time()))
                conn.execute("""
                    DELETE FROM access_log WHERE id NOT IN (
                        SELECT id FROM access_log ORDER BY cached_at DESC LIMIT 1000
                    )
                """)
    except Exception as e:
        warnings.warn(f"ToolRecall: failed to record stats: {e}")


def _record_tokens_read_from_disk(category: str, tokens: int, is_new_file: bool = True):
    """Record tokens read from disk.

    Args:
        category: Cache category name
        tokens: Estimated token count of the content
        is_new_file: True = content was never in cache or mtime changed
                      (counts toward tokens_read_from_disk).
                      False = content was in SQLite but not in-memory
                      (don't double-count — already counted on first read).
    """
    if tokens <= 0:
        return
    try:
        with _db() as conn:
            if is_new_file:
                conn.execute("""
                    INSERT INTO cache_stats (category, hits, misses, tokens_read_from_disk)
                    VALUES (?, 0, 0, ?)
                    ON CONFLICT(category) DO UPDATE SET
                        tokens_read_from_disk = tokens_read_from_disk + ?,
                        updated_at = ?
                """, (category, tokens, tokens, time.time()))
            else:
                conn.execute("""
                    INSERT INTO cache_stats (category, hits, misses, tokens_read_from_disk)
                    VALUES (?, 0, 0, 0)
                    ON CONFLICT(category) DO UPDATE SET updated_at = ?
                """, (category, time.time()))
    except Exception as e:
        warnings.warn(f"ToolRecall: failed to record tokens read from disk: {e}")


# ─── FILE CACHE (hybrid: in-memory LRU + SQLite) ────────────

def _persist_file_to_sqlite(path: str, content: str, stat_result):
    """Write file to SQLite for cross-session persistence.

    Files larger than ~10 MB are excluded from SQLite to keep the DB small.
    They remain in the in-memory LRU only, so they're cached per-session
    but not persisted across daemon restarts.
    """
    path_hash = _hash(path)
    try:
        with _db() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO file_cache (path_hash, path, content, mtime, size, cached_at)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (path_hash, path, content, stat_result.st_mtime, stat_result.st_size, time.time()))
    except Exception as e:
        warnings.warn(f"ToolRecall: SQLite persist failed for {path}: {e}")


def cached_read(path: str) -> dict:
    """Read file with hybrid cache.

    Lookup order: in-memory LRU (fast) → SQLite (persistent) → disk.
    """
    path = os.path.expanduser(path)

    # Reject null bytes — prevents path traversal tricks on some systems
    if "\x00" in path:
        return {"error": "Path not allowed: contains null byte"}

    # Security: reject sensitive paths BEFORE any cache access
    if _is_sensitive_path(path):
        return {"error": "Security: path matches sensitive file pattern, refusing to read."}

    if not os.path.exists(path):
        hint = ""
        basename = os.path.basename(path)
        # Common toolrecall config locations
        if basename in ("toolrecall.toml", "config.toml"):
            for candidate in [
                os.path.expanduser("~/.config/toolrecall/toolrecall.toml"),
                os.path.expanduser("~/.toolrecall/config.toml"),
            ]:
                if os.path.exists(candidate):
                    hint = f" Did you mean: {candidate}?"
                    break
            if not hint:
                hint = " Run \"toolrecall init\" to create a default config."
        return {"error": f"File not found: {path}.{hint}"}

    stat = os.stat(path)

    # ── 1. In-memory cache (fast path) ──
    entry = _file_cache.get(path)
    if entry and entry["mtime"] == stat.st_mtime:
        _record("file_cache", hit=True, path=path, tokens_saved=_estimate_tokens(entry["content"]))
        return {"cached": True, "content": entry["content"], "path": path}

    # ── 2. SQLite cache (warm from previous session) ──
    path_hash = _hash(path)
    try:
        with _db() as conn:
            row = conn.execute(
                "SELECT content, mtime FROM file_cache WHERE path_hash = ?", (path_hash,)
            ).fetchone()
    except Exception as e:
        warnings.warn(f"ToolRecall: SQLite read failed for {path}: {e}")
        row = None

    if row and row["mtime"] == stat.st_mtime:
        _file_cache.put(path, {"content": row["content"], "mtime": row["mtime"], "size": stat.st_size})
        _record("file_cache", hit=True, path=path, tokens_saved=_estimate_tokens(row["content"]))
        return {"cached": True, "content": row["content"], "path": path}  # ── 2. SQLite hit ──
    _record("file_cache", hit=False, path=path)
    # Count tokens_read_from_disk only for truly new content:
    # - brand new file (never in SQLite) → row is None
    # - file was modified (mtime changed) → row exists but stale
    # If row exists with SAME mtime → SQLite hit should have caught it above
    _row_exists = row is not None
    _mtime_changed = _row_exists and row["mtime"] != stat.st_mtime
    _is_new_file = row is None or _mtime_changed

    # ── 3. Cache miss — read from disk ──

    # Security: Hard limit to prevent OOM on huge files (e.g. logs/binaries)
    # 5MB max ~ 1.2M tokens (exceeds most context windows anyway)
    if stat.st_size > 5 * 1024 * 1024:
        return {"error": f"File exceeds 5MB limit ({stat.st_size / 1024 / 1024:.1f} MB). Refusing to cache or read."}

    try:
        # Use the real open() to bypass the shim (if installed).
        # The shim intercepts open() and routes back to cached_read,
        # causing double-counting of stats and unnecessary re-entry.
        # toolrecall.shim saves _original_open = builtins.open before
        # patching; if the shim isn't installed, fall back to builtins.open.
        try:
            from toolrecall.shim import _original_open as _real_open
        except ImportError:
            _real_open = open
        with _real_open(path, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()
    except Exception as e:
        return {"error": str(e)}

    _file_cache.put(path, {"content": content, "mtime": stat.st_mtime, "size": stat.st_size})
    _record_tokens_read_from_disk("file_cache", _estimate_tokens(content), is_new_file=_is_new_file)

    # Large files (>10 MB) go to SQLite only if they're "static data"
    # Small files always persist for cross-session reuse
    if stat.st_size < 10 * 1024 * 1024:
        _persist_file_to_sqlite(path, content, stat)

    return {"cached": False, "content": content, "path": path}


# ─── SKILL CACHE (SQLite + in-memory) ───────────────────────

_skill_cache: dict[str, dict] = {}
_skill_cache_lock = Lock()


def cached_skill(skill_name: str, skill_dirs: list = None) -> dict:
    """Load skill + linked files with cache."""
    if skill_dirs is None:
        skill_dirs = load_config().skill_dirs

    skill_path = None
    for base in skill_dirs:
        base = os.path.expanduser(base)
        if not os.path.exists(base):
            continue
        for cat in os.listdir(base):
            cat_path = os.path.join(base, cat, skill_name)
            if os.path.exists(os.path.join(cat_path, "SKILL.md")):
                skill_path = cat_path
                break
        if skill_path:
            break

    if not skill_path:
        return {"error": f"Skill not found: {skill_name}"}

    skill_files = []
    for root, dirs, files in os.walk(skill_path):
        for f in files:
            full = os.path.join(root, f)
            st = os.stat(full)
            skill_files.append({
                "path": full,
                "rel": os.path.relpath(full, skill_path),
                "mtime": st.st_mtime,
                "size": st.st_size,
            })

    if not skill_files:
        return {"error": f"No files found in skill: {skill_name}"}

    max_mtime = max(f["mtime"] for f in skill_files) if skill_files else 0
    with _skill_cache_lock:
        mem = _skill_cache.get(skill_name)
        if mem and mem.get("cached_at", 0) >= max_mtime:
            _record("skill_cache", hit=True)
            return {"cached": True, "content": mem["content"], "skill": skill_name, "files": len(skill_files)}

    try:
        with _db() as conn:
            row = conn.execute(
                "SELECT content, cached_at FROM skill_cache WHERE skill_name = ?",
                (skill_name,)
            ).fetchone()
    except Exception as e:
        warnings.warn(f"ToolRecall: SQLite skill read failed: {e}")
        row = None

    if row and row.get("cached_at", 0) >= max_mtime:
        with _skill_cache_lock:
            _skill_cache[skill_name] = {"content": row["content"], "cached_at": row["cached_at"]}
        _record("skill_cache", hit=True)
        return {"cached": True, "content": row["content"], "skill": skill_name, "files": len(skill_files)}

    _record("skill_cache", hit=False)
    parts = []
    for sf in skill_files:
        try:
            with open(sf["path"], "r", encoding="utf-8", errors="ignore") as fh:
                parts.append(f"--- {sf['rel']} ---\n{fh.read()}")
        except Exception:
            continue

    content = "\n\n".join(parts)
    now = time.time()

    with _skill_cache_lock:
        _skill_cache[skill_name] = {"content": content, "cached_at": now}
    _record_tokens_read_from_disk("skill_cache", _estimate_tokens(content))

    try:
        with _db() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO skill_cache (skill_name, content, file_count, cached_at)
                VALUES (?, ?, ?, ?)
            """, (skill_name, content, len(skill_files), now))
    except Exception as e:
        warnings.warn(f"ToolRecall: SQLite skill persist failed: {e}")

    return {"cached": False, "content": content, "skill": skill_name, "files": len(skill_files)}


# ─── TERMINAL CACHE (SQLite — output not on disk) ──────────

DEFAULT_CACHEABLE = {
    # ── System info (very static) ────────────────
    "hostname": 3600,
    "whoami": 3600,
    "pwd": 3600,
    "uname -a": 3600,

    # ── State checkers (moderate TTL) ────────────
    "uptime": 300,
    "free -h": 300,
    "df -h /": 300,
    "crontab -l": 3600,

    # ── File listing (60s — directory contents stable)
    "ls": 60,
    "ls -la": 60,
    "ls -1": 60,
    "ls -l": 60,
    "ls -lh": 60,

    # ── File reading (30s — avoids re-reads during same reasoning step)
    "cat": 30,
    "head": 30,
    "tail": 30,
    "wc": 30,

    # ── Searching (60s — results stable within agent turn)
    "grep": 60,
    "rg": 60,
    "find": 60,
    "fd": 60,

    # ── Git read-only (30s — changes frequently, short TTL)
    "git status": 30,
    "git diff": 30,
    "git diff --stat": 30,
    "git log --oneline": 30,
    "git log --oneline -5": 30,
    "git branch": 300,
    "git stash list": 300,

    # ── Process snapshots (15s — changes every second)
    "ps aux": 15,
    "ps afx": 15,
    "lsof": 15,

    # ── Disk / memory (120s — changes slowly)
    "du -sh": 120,
    "du -sh *": 120,
    "df -h": 120,

    # ── Environment queries (static within session)
    "which": 3600,
    "python3 --version": 3600,
    "node --version": 3600,
    "pip list": 600,

    # ── Date / time (60s — predictable)
    "date": 60,
    "date +%s": 60,
    "cal": 60,
}


def _match_terminal(cmd: str, pattern: str) -> bool:
    """Match a command against a cacheable pattern.

    - Multi-word patterns (e.g. "git status") require exact match.
    - Single-word patterns (e.g. "hostname", "ls") match by prefix.
      This means "ls -la" matches "ls", "cat /etc/hostname" matches "cat".
    - Safe because DEFAULT_CACHEABLE only contains read-only commands.
      Dangerous commands (rm, sudo, mv, git push, kill) are NOT in the list.
      Users can always bypass with ttl=0.
    """
    cmd_norm = " ".join(cmd.strip().split())
    pattern_norm = " ".join(pattern.strip().split())
    if " " in pattern_norm:
        # Multi-word pattern: exact match only
        return cmd_norm == pattern_norm
    # Single-word pattern: match by prefix
    # "cat /etc/hostname" starts with "cat " — matches
    # "ls -la" starts with "ls " — matches
    # "lsof" matches "ls"? No: "lsof " doesn't start with "ls ", and "lsof" != "ls"
    return cmd_norm == pattern_norm or cmd_norm.startswith(pattern_norm + " ")


_LOG_SHELL_FALLBACK = str(config.get("cache", "log_shell_fallback", default="true") or "true").lower() == "true"


def _log_shell_fallback(cmd: str, fallback_type: str = "shell"):
    """Log when shell=True fallback is used (security audit signal)."""
    import logging
    logging.getLogger("toolrecall.cache").warning(
        "shell=True fallback (%s): %.200s", fallback_type, cmd
    )


def cached_terminal(command: str, ttl: int = None) -> dict:
    """Run command OR return cached result (TTL-based, SQLite-backed).

    Only commands that match a known-cacheable pattern exactly are cached.
    All other commands execute every time (no cache, no delay).
    """
    import subprocess
    import shlex

    cmd = " ".join(command.strip().split())
    cacheable_ttl = ttl

    all_ttls = dict(DEFAULT_CACHEABLE)
    config_ttls = config.get("cache", "terminal_ttls", default={})
    if isinstance(config_ttls, dict):
        all_ttls.update(config_ttls)

    is_cacheable = False
    for pattern, t in all_ttls.items():
        if _match_terminal(cmd, pattern):
            is_cacheable = True
            if cacheable_ttl is None:
                cacheable_ttl = t
            break

    if not is_cacheable:
        # SECURITY: Never use shell=True — command injection risk.
        # Use shlex.split to pass args as a list to subprocess.
        # If shlex fails (complex shell syntax), the caller should use
        # a shell script file via cached_run() instead.
        try:
            cmd_parts = shlex.split(cmd, posix=_POSIX_MODE)
            result = subprocess.run(cmd_parts, capture_output=True, text=True, timeout=30)
        except (ValueError, OSError) as e:
            return {"error": f"Cannot parse command: {e}", "exit_code": -1, "cached": False}
        return {"output": result.stdout, "exit_code": result.returncode, "cached": False}

    # Normalize command for cache key when enabled
    cmd_key = normalize_command(cmd) if config.get("norm", "enabled", default=False) else cmd
    cmd_hash = _hash(cmd_key)
    now = time.time()

    try:
        with _db() as conn:
            row = conn.execute(
                "SELECT output, exit_code, expires_at FROM terminal_cache WHERE command_hash = ?",
                (cmd_hash,)
            ).fetchone()
            if row and row["expires_at"] > now:
                conn.execute("UPDATE terminal_cache SET hits = hits + 1 WHERE command_hash = ?", (cmd_hash,))
                _record("terminal_cache", hit=True)
                return {"output": row["output"], "exit_code": row["exit_code"], "cached": True}
    except Exception as e:
        warnings.warn(f"ToolRecall: SQLite terminal read failed: {e}")

    _record("terminal_cache", hit=False)
    # Use shlex.split for cacheable commands — avoids shell injection
    # e.g. cached_terminal("git status; rm -rf /") → ["git", "status; rm -rf /"] → fails safely
    try:
        cmd_parts = shlex.split(cmd, posix=_POSIX_MODE)
        result = subprocess.run(cmd_parts, capture_output=True, text=True, timeout=30)
    except (ValueError, OSError):
        # SECURITY: shlex.split failed — do NOT fall back to shell=True.
        # Return an error instead of risking command injection.
        if _LOG_SHELL_FALLBACK:
            _log_shell_fallback(cmd, "shlex split failed (terminal)")
        return {"error": "Command contains unparseable shell syntax. Use a script file instead.", "exit_code": -1, "cached": False}

    expires = now + (cacheable_ttl or 300)
    try:
        with _db() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO terminal_cache (command_hash, command, output, exit_code, cached_at, expires_at)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (cmd_hash, cmd, result.stdout, result.returncode, now, expires))
    except Exception as e:
        warnings.warn(f"ToolRecall: SQLite terminal persist failed: {e}")

    _record_tokens_read_from_disk("terminal_cache", _estimate_tokens(result.stdout))

    return {"output": result.stdout, "exit_code": result.returncode, "cached": False}


# ─── SCRIPT CACHE (SQLite + mtime) ─────────────────────────

SCRIPT_CACHEABLE_EXTENSIONS = {".py", ".sh", ".bash", ".js", ".ts", ".rs", ".go", ".rb", ".pl"}


def cached_run(script_path: str, args: str = "", ttl: int = 0) -> dict:
    """Run a script file WITH cache (mtime + TTL, SQLite-backed).

    Uses shlex.split() for cacheable scripts — safer than shell=True.
    """
    import subprocess
    import shlex

    path = os.path.expanduser(script_path)
    if not os.path.exists(path):
        return {"error": f"Script not found: {path}"}

    stat = os.stat(path)
    ext = os.path.splitext(path)[1].lower()
    if ttl is not None and ttl <= 0:
        # SECURITY: Use shlex.split instead of shell=True.
        # The path is validated (exists check above); args are split safely.
        try:
            run_args = shlex.split(args, posix=_POSIX_MODE) if args else []
            result = subprocess.run([path] + run_args, capture_output=True, text=True, timeout=60)
        except (ValueError, OSError) as e:
            return {"error": f"Cannot parse script arguments: {e}", "exit_code": -1}
        return {"output": result.stdout, "exit_code": result.returncode, "cached": False}

    is_cacheable = ext in SCRIPT_CACHEABLE_EXTENSIONS

    if not is_cacheable:
        # SECURITY: Use shlex.split instead of shell=True.
        if _LOG_SHELL_FALLBACK:
            _log_shell_fallback(f"{path} {args}", "non-cacheable script")
        try:
            run_args = shlex.split(args, posix=_POSIX_MODE) if args else []
            result = subprocess.run([path] + run_args, capture_output=True, text=True, timeout=60)
        except (ValueError, OSError) as e:
            return {"error": f"Cannot parse script arguments: {e}", "exit_code": -1}
        return {"output": result.stdout, "exit_code": result.returncode, "cached": False}

    # Normalize script path+args for cache key when enabled
    script_key = f"{path}:{normalize_command(args)}" if config.get("norm", "enabled", default=False) else f"{path}:{args}"
    path_hash = _hash(script_key)
    now = time.time()

    try:
        with _db() as conn:
            row = conn.execute(
                "SELECT output, exit_code, cached_at FROM script_cache WHERE script_hash = ?",
                (path_hash,)
            ).fetchone()
    except Exception as e:
        warnings.warn(f"ToolRecall: SQLite script read failed: {e}")
        row = None

    valid = False
    if row:
        age = now - row["cached_at"]
        if age < ttl and stat.st_mtime < row["cached_at"]:
            valid = True

    if valid:
        _record("script_cache", hit=True)
        return {"output": row["output"], "exit_code": row["exit_code"], "cached": True}

    _record("script_cache", hit=False)
    try:
        # Use shlex.split for cacheable scripts
        script_args = shlex.split(args, posix=_POSIX_MODE) if args else []
        cmd = [path] + script_args
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except (ValueError, OSError):
        # SECURITY: shlex.split failed — do NOT fall back to shell=True.
        if _LOG_SHELL_FALLBACK:
            _log_shell_fallback(f"{path} {args}", "shlex split failed (script)")
        return {"error": "Script arguments contain unparseable shell syntax.", "exit_code": -1}

    try:
        with _db() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO script_cache (script_hash, script_path, args, output, exit_code, cached_at)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (path_hash, path, args, result.stdout, result.returncode, now))
    except Exception as e:
        warnings.warn(f"ToolRecall: SQLite script persist failed: {e}")

    _record_tokens_read_from_disk("script_cache", _estimate_tokens(result.stdout))

    return {"output": result.stdout, "exit_code": result.returncode, "cached": False}


# ─── CODE CACHE (SQLite + content hash) ─────────────────────

def cached_exec(code: str, ttl: int = 0) -> dict:
    """Execute Python code string WITH cache by content hash (SQLite-backed)."""
    import subprocess

    if ttl is not None and ttl <= 0:
        result = subprocess.run(
            ["python3", "-c", code],
            capture_output=True, text=True, timeout=30
        )
        return {"output": result.stdout, "exit_code": result.returncode, "cached": False}

    code_hash = _hash(code)
    now = time.time()

    try:
        with _db() as conn:
            row = conn.execute(
                "SELECT output, exit_code, cached_at FROM code_cache WHERE code_hash = ?",
                (code_hash,)
            ).fetchone()
    except Exception as e:
        warnings.warn(f"ToolRecall: SQLite code read failed: {e}")
        row = None

    valid = False
    if row:
        age = now - row["cached_at"]
        if age < ttl:
            valid = True

    if valid:
        _record("code_cache", hit=True)
        return {"output": row["output"], "exit_code": row["exit_code"], "cached": True}

    _record("code_cache", hit=False)
    result = subprocess.run(
        ["python3", "-c", code],
        capture_output=True, text=True, timeout=30
    )

    try:
        with _db() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO code_cache (code_hash, code, output, exit_code, cached_at)
                VALUES (?, ?, ?, ?, ?)
            """, (code_hash, code, result.stdout, result.returncode, now))
    except Exception as e:
        warnings.warn(f"ToolRecall: SQLite code persist failed: {e}")

    _record_tokens_read_from_disk("code_cache", _estimate_tokens(result.stdout))

    return {"output": result.stdout, "exit_code": result.returncode, "cached": False}


# ─── WRITE CACHE (content-hash dedup, skip identical writes) ──
#
# cached_write: write a file only if content differs from disk.
#   Returns {"unchanged": True, "path": ...} when content matches.
#   Returns {"cached": False, "path": ...} when write actually happened.
#
# cached_patch: apply a find-and-replace only if not already applied.
#   Returns {"unchanged": True, "reason": "already_applied"|"not_found", ...}
#   Returns {"cached": False, ...} when patch actually happened.
#
# Both are idempotency checks — they save output tokens by skipping
# redundant work, and reduce agent loop-waste.
#
# ⚠️  TOCTOU: content comparison is at read-time only. External
#    mutations between the check and the write are not detected.
# ⚠️  Metadata-agnostic: chmod/chown-only changes are not detected.
# ⚠️  Read overhead: adds a read+hash before every write.
#    For large files (>5MB) both operations auto-fall through.
# ⚠️  Not persisted to SQLite — stateless per-call decision.
# ─────────────────────────────────────────────────────────────

WRITE_CACHE_MAX_BYTES = 5 * 1024 * 1024  # 5MB — same as file cache limit


def cached_write(path: str, content: str) -> dict:
    """Write a file, skipping if content is identical to disk.

    Args:
        path: File path to write.
        content: Content to write.

    Returns:
        On content-match:  {"unchanged": True, "path": path}
        On new write:      {"cached": False, "path": path, "size": N}
        On error:          {"error": "...", "path": path}
    """
    path = os.path.expanduser(path)

    # Create parent directories if needed
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)

    # File doesn't exist yet — always write
    if not os.path.exists(path):
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
            _file_cache.remove(path)
            _record("write_cache", hit=False)
            return {"cached": False, "path": path, "size": len(content)}
        except Exception as e:
            return {"error": str(e), "path": path}

    # File exists — check size threshold
    try:
        stat = os.stat(path)
    except OSError as e:
        return {"error": str(e), "path": path}

    if stat.st_size > WRITE_CACHE_MAX_BYTES:
        # Skip dedup for large files — read would cost more than write
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
            _file_cache.remove(path)
            _record("write_cache", hit=False)
            return {"cached": False, "path": path, "size": len(content)}
        except Exception as e:
            return {"error": str(e), "path": path}

    # Read current content and compare
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            current = f.read()
    except Exception as e:
        return {"error": str(e), "path": path}

    if current == content:
        _record("write_cache", hit=True)
        _record_tokens_read_from_disk("write_cache", _estimate_tokens(content))
        return {"unchanged": True, "path": path}

    # Content differs — write
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        _file_cache.remove(path)  # Invalidate stale cache entry (mtime may not change on fast writes)
        _record("write_cache", hit=False)
        return {"cached": False, "path": path, "size": len(content)}
    except Exception as e:
        return {"error": str(e), "path": path}


def cached_patch(path: str, old_string: str, new_string: str) -> dict:
    """Apply a find-and-replace patch, skipping if already applied or missing.

    Three outcomes:
      - new_string already in place → {"unchanged": True, "reason": "already_applied"}
      - old_string not found        → {"unchanged": True, "reason": "not_found"}
      - patch applied               → {"cached": False, "path": path, "changes": N}

    ⚠️  Same TOCTOU caveat as cached_write — comparison is read-time only.
    ⚠️  If old_string appears multiple times, only the first is checked.
    """
    path = os.path.expanduser(path)

    if not os.path.exists(path):
        return {"error": f"File not found: {path}", "path": path}
    # Read current content
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()
    except Exception as e:
        return {"error": str(e), "path": path}

    # Check: is new_string already in place (regardless of old_string)?
    if new_string in content:
        if old_string not in content:
            # new_string is there, old_string isn't → likely already applied
            _record("patch_cache", hit=True)
            _record_tokens_read_from_disk("patch_cache", _estimate_tokens(new_string))
            return {"unchanged": True, "reason": "already_applied", "path": path}
        # Both present — check position
        idx_old = content.find(old_string)
        idx_new = content.find(new_string)
        if idx_new <= idx_old:
            # new_string appears at or before old_string — already applied
            _record("patch_cache", hit=True)
            _record_tokens_read_from_disk("patch_cache", _estimate_tokens(new_string))
            return {"unchanged": True, "reason": "already_applied", "path": path}

    # Check: is old_string present at all?
    if old_string not in content:
        _record("patch_cache", hit=True)
        return {"unchanged": True, "reason": "not_found", "path": path}

    # Apply the patch
    change_count = content.count(old_string)
    new_content = content.replace(old_string, new_string, 1)  # match Hermes' single-replace

    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(new_content)
        _file_cache.remove(path)  # Invalidate stale cache entry (mtime may not change on fast writes)
        _record("patch_cache", hit=False)
        return {"cached": False, "path": path, "changes": change_count}
    except Exception as e:
        return {"error": str(e), "path": path}


# ─── MCP CACHE (SQLite, TTL-based) ─────────────────────────

MCP_DEFAULT_TTL = config.get("mcp", "default_ttl", default=60)
_POSIX_MODE = os.name != 'nt'  # shlex.split: posix=True on Unix, False on Windows


def cached_mcp_check(server: str, tool: str, arguments: dict = None, ttl: int = None) -> dict:
    """Check if an MCP tool call result is cached.

    Returns cached data on hit, or a miss indicator with the cache key.
    The caller makes the real MCP call on miss, then calls cached_mcp_store().

    Set ttl=0 to bypass the cache entirely.
    """
    import json as _json

    # ttl=0 means bypass cache
    if ttl is not None and ttl <= 0:
        _record("mcp_cache", hit=False)
        args_json = _json.dumps(arguments, sort_keys=True) if arguments else "{}"
        request_str = f"{server}://{tool}?{args_json}"
        request_hash = _hash(request_str)
        return {"cached": False, "key": request_hash, "bypassed": True, "server": server, "tool": tool}

    ttl = ttl if ttl is not None else MCP_DEFAULT_TTL

    # Normalize MCP arguments for cache key when enabled
    if config.get("norm", "enabled", default=False) and arguments:
        from toolrecall.normalizer import normalize_tool_args
        args_json = normalize_tool_args(arguments)
    else:
        args_json = _json.dumps(arguments, sort_keys=True) if arguments else "{}"
    request_str = f"{server}://{tool}?{args_json}"
    request_hash = _hash(request_str)
    now = time.time()

    try:
        with _db() as conn:
            row = conn.execute(
                "SELECT data, expires_at FROM mcp_cache WHERE request_hash = ?",
                (request_hash,)
            ).fetchone()
            if row and row["expires_at"] > now:
                conn.execute("UPDATE mcp_cache SET hits = hits + 1 WHERE request_hash = ?", (request_hash,))
                _record("mcp_cache", hit=True)
                return {"cached": True, "data": row["data"], "server": server, "tool": tool}
    except Exception as e:
        warnings.warn(f"ToolRecall: MCP cache read failed: {e}")

    _record("mcp_cache", False)
    return {"cached": False, "key": request_hash, "server": server, "tool": tool}


def cached_mcp_store(request_hash: str, server: str, tool: str, arguments: dict, data: str, ttl: int = None):
    """Store an MCP tool call result for future cache hits."""
    import json as _json
    ttl = ttl if ttl is not None else MCP_DEFAULT_TTL
    now = time.time()
    expires = now + ttl
    args_json = _json.dumps(arguments, sort_keys=True) if arguments else "{}"
    try:
        with _db() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO mcp_cache 
                (request_hash, mcp_server, mcp_tool, arguments, data, cached_at, expires_at) 
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (request_hash, server, tool, args_json, data, now, expires))
    except Exception as e:
        warnings.warn(f"ToolRecall: MCP cache store failed: {e}")

    _record_tokens_read_from_disk("mcp_cache", _estimate_tokens(data))

def invalidate_mcp_server(server: str):
    """Invalidate all cached items for a specific MCP server."""
    try:
        with _db() as conn:
            conn.execute("DELETE FROM mcp_cache WHERE mcp_server = ?", (server,))
    except Exception as e:
        warnings.warn(f"ToolRecall: MCP server invalidate failed: {e}")

def cached_mcp(server: str, tool: str, arguments: dict = None,
               fetch_fn: callable = None, ttl: int = None) -> dict:
    """One-shot MCP cache: check → (optional) fetch → store → return.

    Usage:
        data = cached_mcp("fetch", "fetch", {"url": "..."},
                          fetch_fn=lambda: requests.get(url).json())
    """
    import json as _json
    result = cached_mcp_check(server, tool, arguments, ttl)
    if result.get("cached"):
        return _json.loads(result["data"])
    if fetch_fn is not None:
        data = fetch_fn()
        cached_mcp_store(result["key"], server, tool, arguments, _json.dumps(data), ttl)
        return data
    return result


# ─── STATS & ADMIN ─────────────────────────────────────────

def get_stats() -> dict:
    """Get cache statistics (from SQLite + in-memory)."""
    stats = {}
    try:
        with _db() as conn:
            for row in conn.execute("SELECT * FROM cache_stats"):
                total = row["hits"] + row["misses"]
                stats[row["category"]] = {
                    "hits": row["hits"],
                    "misses": row["misses"],
                    "tokens_read_from_disk": row["tokens_read_from_disk"],
                    "tokens_saved": row["tokens_saved"],
                    "updated_at": row["updated_at"],
                    "hit_rate": f"{row['hits']/total*100:.0f}%" if total > 0 else "0%",
                }
            for t in ["file_cache", "skill_cache", "terminal_cache", "script_cache", "code_cache", "mcp_cache", "browser_cache"]:
                r = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()
                stats[f"{t}_entries"] = r[0]
    except Exception as e:
        warnings.warn(f"ToolRecall: SQLite stats failed: {e}")

    stats["memory_file_entries"] = len(_file_cache)
    stats["memory_used_mb"] = round(_file_cache.memory_bytes() / (1024 * 1024), 2)
    stats["memory_max_mb"] = MAX_MEMORY_MB
    with _skill_cache_lock:
        stats["memory_skill_entries"] = len(_skill_cache)

    # Recent access log (last 20 entries)
    try:
        with _db() as conn:
            rows = conn.execute(
                "SELECT category, path, hit, tokens, cached_at "
                "FROM access_log ORDER BY cached_at DESC LIMIT 20"
            ).fetchall()
            stats["recent"] = [
                {
                    "category": r["category"],
                    "path": r["path"],
                    "hit": bool(r["hit"]),
                    "tokens": r["tokens"],
                    "since_status": f"{time.time() - r['cached_at']:.1f}s",
                    "cached_at": datetime.fromtimestamp(r["cached_at"]).isoformat(),
                }
                for r in rows
            ]
    except Exception:
        stats["recent"] = []

    return stats


def reset_stats():
    """Reset cache statistics counters (hits, misses, tokens_read_from_disk, tokens_saved) without clearing cache entries."""
    try:
        with _db() as conn:
            conn.execute("DELETE FROM cache_stats")
            conn.execute("DELETE FROM access_log")
    except Exception as e:
        warnings.warn(f"ToolRecall: reset_stats failed: {e}")


def invalidate_all():
    """Clear ALL caches (memory + SQLite)."""
    _file_cache.clear()
    with _skill_cache_lock:
        _skill_cache.clear()

    try:
        with _db() as conn:
            conn.execute("DELETE FROM file_cache")
            conn.execute("DELETE FROM skill_cache")
            conn.execute("DELETE FROM terminal_cache")
            conn.execute("DELETE FROM script_cache")
            conn.execute("DELETE FROM code_cache")
            conn.execute("DELETE FROM mcp_cache")
            conn.execute("DELETE FROM browser_cache")
    except Exception as e:
        warnings.warn(f"ToolRecall: SQLite invalidate failed: {e}")


def invalidate_file(path: str):
    """Invalidate a specific file from cache (memory + SQLite)."""
    path = os.path.expanduser(path)
    _file_cache.remove(path)

    h = _hash(path)
    try:
        with _db() as conn:
            conn.execute("DELETE FROM file_cache WHERE path_hash = ?", (h,))
    except Exception as e:
        warnings.warn(f"ToolRecall: SQLite file invalidate failed: {e}")


_init()  # Uses re-exported wrapper with SCHEMA

def refresh_file(path: str) -> dict:
    """Invalidate cache for a file and re-read it from disk in one call.

    Combines invalidate_file() + cached_read() so the caller always
    gets a fresh result.  Useful for manual refresh during a session
    or after an external edit.

    Returns the same dict as cached_read() — {"content": ..., "cached": False}
    (always "cached": False since we force a fresh read).
    """
    from toolrecall.cache import invalidate_file
    invalidate_file(path)
    return cached_read(path)


def garbage_collect() -> int:
    """Remove expired cache entries, vacuum database, and reset stale stats.

    Cache stats are reset every 24 hours to keep the hit/miss/read counters
    representative of recent activity rather than cumulative since DB creation.
    The updated_at timestamp tracks when each category was last reset.
    """
    import time
    try:
        with _db() as conn:
            now = time.time()
            c1 = conn.execute("DELETE FROM terminal_cache WHERE expires_at < ?", (now,)).rowcount
            c2 = conn.execute("DELETE FROM mcp_cache WHERE expires_at < ?", (now,)).rowcount
            ONE_DAY = 86400
            c3 = conn.execute("""
                UPDATE cache_stats SET
                    hits = 0,
                    misses = 0,
                    tokens_read_from_disk = 0,
                    tokens_saved = 0,
                    updated_at = ?
                WHERE updated_at > 0 AND updated_at < ?
            """, (now, now - ONE_DAY)).rowcount
        # VACUUM must run in its own connection (auto-commits, can't be in a transaction)
        with _db() as conn:
            conn.execute("VACUUM")
        return c1 + c2 + c3
    except Exception as e:
        warnings.warn(f"ToolRecall GC failed: {e}")
        return -1


# ─── BROWSER PAGE CACHE (SQLite, key-value) ──────────────

BROWSER_CACHE_TTL = 3600  # 1 hour default for browser page cache


def cached_browser_check(
    cache_key: str,
) -> dict:
    """Check if browser page content is cached by key.

    Args:
        cache_key: e.g. ``browser:page:https_example_com:snapshot``

    Returns:
        ``{"cached": True, "content": "...", "tokens_saved": N}`` on hit,
        ``{"cached": False}`` on miss.
    """
    try:
        with _db() as conn:
            row = conn.execute(
                "SELECT content, hits FROM browser_cache WHERE cache_key = ?",
                (cache_key,),
            ).fetchone()
            if row:
                conn.execute(
                    "UPDATE browser_cache SET hits = hits + 1 WHERE cache_key = ?",
                    (cache_key,),
                )
                _record("browser_cache", True)
                tokens_saved = _estimate_tokens(row["content"])
                return {
                    "cached": True,
                    "content": row["content"],
                    "tokens_saved": tokens_saved,
                }
    except Exception as e:
        warnings.warn(f"ToolRecall: browser_cache check failed: {e}")

    _record("browser_cache", False)
    return {"cached": False}


def cached_browser_store(
    cache_key: str,
    content: str,
    url: str = "",
    content_type: str = "snapshot",
    title: str = "",
    content_hash: str = "",
) -> dict:
    """Store browser page content in the cache.

    Args:
        cache_key: unique cache key (e.g. ``browser:page:https_example_com:snapshot``)
        content: page content (HTML, text, or snapshot)
        url: original URL for metadata
        content_type: ``html``, ``text``, or ``snapshot``
        title: page title for metadata
        content_hash: for change detection

    Returns:
        ``{"stored": True}`` on success, ``{"stored": False, "error": "..."}`` on failure.
    """
    import time

    # Defense in depth: limit content size at the cache layer.
    # The HTTP proxy also enforces this, but UDS/MCP paths bypass the proxy.
    MAX_CONTENT_BYTES = 5 * 1024 * 1024  # 5 MB
    if len(content) > MAX_CONTENT_BYTES:
        return {
            "stored": False,
            "error": f"Content too large ({len(content)} bytes, max {MAX_CONTENT_BYTES})",
        }
    try:
        with _db() as conn:
            now = time.time()
            conn.execute(
                """INSERT OR REPLACE INTO browser_cache
                   (cache_key, url, content_type, content, title, content_hash, cached_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (cache_key, url, content_type, content, title, content_hash, now),
            )
        _record_tokens_read_from_disk("browser_cache", _estimate_tokens(content))
        return {"stored": True}
    except Exception as e:
        warnings.warn(f"ToolRecall: browser_cache store failed: {e}")
        return {"stored": False, "error": str(e)}


def invalidate_browser_url(url: str):
    """Invalidate all cached entries for a specific URL."""
    try:
        with _db() as conn:
            conn.execute("DELETE FROM browser_cache WHERE url = ?", (url,))
    except Exception as e:
        warnings.warn(f"ToolRecall: browser_cache invalidate failed: {e}")


# ─── API CACHE (forward-proxy responses, keyed by request hash) ─────

API_CACHE_TTL = 300  # 5 minutes default for API responses


def cached_api_check(request_hash: str) -> dict:
    """Check if an API response is cached by request hash.

    Args:
        request_hash: SHA256 hash of ``method:host:path:body``

    Returns:
        ``{"cached": True, "status": 200, "headers": {...}, "body": "..."}`` on hit,
        ``{"cached": False}`` on miss.
    """
    import time
    try:
        with _db() as conn:
            now = time.time()
            row = conn.execute(
                "SELECT response_status, response_headers, response_body, expires_at "
                "FROM api_cache WHERE request_hash = ?",
                (request_hash,),
            ).fetchone()
            if row and row["expires_at"] > now:
                conn.execute(
                    "UPDATE api_cache SET hits = hits + 1 WHERE request_hash = ?",
                    (request_hash,),
                )
                _record("api_cache", True)
                import json as _json
                headers = _json.loads(row["response_headers"]) if row["response_headers"] else {}
                tokens_saved = _estimate_tokens(row["response_body"])
                return {
                    "cached": True,
                    "status": row["response_status"],
                    "headers": headers,
                    "body": row["response_body"],
                    "tokens_saved": tokens_saved,
                }
    except Exception as e:
        warnings.warn(f"ToolRecall: api_cache check failed: {e}")

    _record("api_cache", False)
    return {"cached": False}


def cached_api_store(request_hash: str, method: str, host: str, path: str,
                     request_body_hash: str, response_status: int,
                     response_headers: dict, response_body: str,
                     ttl: int = None) -> dict:
    """Store an API response in the cache.

    Args:
        request_hash: SHA256 of ``method:host:path:body``
        method: HTTP method
        host: upstream host (e.g. ``api.openai.com``)
        path: request path (e.g. ``/v1/chat/completions``)
        request_body_hash: SHA256 of just the body (for debugging)
        response_status: HTTP status code
        response_headers: response headers dict
        response_body: response body string
        ttl: TTL in seconds (default: API_CACHE_TTL = 300)

    Returns:
        ``{"stored": True}`` on success
    """
    import json as _json
    import time
    ttl = ttl if ttl is not None else API_CACHE_TTL
    now = time.time()
    expires = now + ttl

    try:
        with _db() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO api_cache
                   (request_hash, method, host, path, request_body_hash,
                    request_body_preview, response_status, response_headers,
                    response_body, cached_at, expires_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (request_hash, method, host, path, request_body_hash,
                 response_body[:200], response_status,
                 _json.dumps(response_headers), response_body, now, expires),
            )
        _record_tokens_read_from_disk("api_cache", _estimate_tokens(response_body))
        return {"stored": True}
    except Exception as e:
        warnings.warn(f"ToolRecall: api_cache store failed: {e}")
        return {"stored": False, "error": str(e)}


def invalidate_api_host(host: str) -> int:
    """Invalidate all cached entries for an API host.

    Returns number of deleted rows.
    """
    try:
        with _db() as conn:
            deleted = conn.execute("DELETE FROM api_cache WHERE host = ?", (host,)).rowcount
            return deleted
    except Exception as e:
        warnings.warn(f"ToolRecall: api_cache invalidate failed: {e}")
        return 0

