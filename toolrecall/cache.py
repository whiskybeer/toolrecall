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
  │  SQLite (file_cache table)           │  ← ~7ms, once per file
  │  Persists across sessions            │
  │  Serves HTTP proxy mode              │
  └──────────────────────────────────────┘

Secondary caches (terminal, script, code) use SQLite directly.

Token estimation: len(content) // 3  →  approximates typical LLM tokenizer
(English ~4 chars/token, code ~2 char/token → weighted average ~3)
"""
import os, sqlite3, time, hashlib, warnings
from pathlib import Path
from threading import Lock
from collections import OrderedDict
from toolrecall.config import load_config

config = load_config()

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
    tokens_intercepted INTEGER DEFAULT 0
);
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
"""


def _get_db():
    cfg = load_config()
    if cfg.storage_backend != "sqlite":
        warnings.warn(f"ToolRecall: Backend '{cfg.storage_backend}' not yet implemented. Falling back to 'sqlite'.")

    db_path = os.path.expanduser(cfg.cache_db)
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30.0)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.row_factory = sqlite3.Row
    return conn


def _init():
    conn = _get_db()
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()


def _estimate_tokens(text: str) -> int:
    """Estimate LLM token count: code-heavy ~2 char/token, English ~4.
    Weighted average ~3 char/token — better than len//4 for agent content.
    """
    if not text:
        return 0
    return max(1, len(text) // 3)


def _record(category, hit: bool, tokens_intercepted: int = 0):
    """Track cache statistics."""
    conn = _get_db()
    try:
        if hit:
            conn.execute("""
                INSERT INTO cache_stats (category, hits, misses, tokens_intercepted)
                VALUES (?, 1, 0, 0)
                ON CONFLICT(category) DO UPDATE SET hits = hits + 1
            """, (category,))
        else:
            conn.execute("""
                INSERT INTO cache_stats (category, hits, misses, tokens_intercepted)
                VALUES (?, 0, 1, 0)
                ON CONFLICT(category) DO UPDATE SET misses = misses + 1
            """, (category,))
        if tokens_intercepted:
            conn.execute("""
                INSERT INTO cache_stats (category, hits, misses, tokens_intercepted)
                VALUES (?, 0, 0, ?)
                ON CONFLICT(category) DO UPDATE SET tokens_intercepted = tokens_intercepted + ?
            """, (category, tokens_intercepted, tokens_intercepted))
        conn.commit()
    except Exception as e:
        warnings.warn(f"ToolRecall: failed to record stats: {e}")
    finally:
        conn.close()


def _record_tokens_saved(category: str, tokens: int):
    """Record tokens saved on a cache miss → disk-read (single count).

    Unlike _record(hit=True, tokens_intercepted=...), this is called
    exactly once per cache entry — on the first disk read that populates
    the cache. Subsequent cache hits (SQLite or In-Memory) only increment
    the hit counter, never re-count the token savings.
    """
    if tokens <= 0:
        return
    conn = _get_db()
    try:
        conn.execute("""
            INSERT INTO cache_stats (category, hits, misses, tokens_intercepted)
            VALUES (?, 0, 0, ?)
            ON CONFLICT(category) DO UPDATE SET tokens_intercepted = tokens_intercepted + ?
        """, (category, tokens, tokens))
        conn.commit()
    except Exception as e:
        warnings.warn(f"ToolRecall: failed to record tokens saved: {e}")
    finally:
        conn.close()


# ─── FILE CACHE (hybrid: in-memory LRU + SQLite) ────────────

def _persist_file_to_sqlite(path: str, content: str, stat_result):
    """Write file to SQLite for cross-session persistence."""
    path_hash = hashlib.md5(path.encode()).hexdigest()
    try:
        conn = _get_db()
        conn.execute("""
            INSERT OR REPLACE INTO file_cache (path_hash, path, content, mtime, size, cached_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (path_hash, path, content, stat_result.st_mtime, stat_result.st_size, time.time()))
        conn.commit()
        conn.close()
    except Exception as e:
        warnings.warn(f"ToolRecall: SQLite persist failed for {path}: {e}")


def cached_read(path: str) -> dict:
    """Read file with hybrid cache.

    Lookup order: in-memory LRU (fast) → SQLite (persistent) → disk.
    """
    path = os.path.expanduser(path)
    if not os.path.exists(path):
        return {"error": f"File not found: {path}"}

    stat = os.stat(path)

    # ── 1. In-memory cache (fast path) ──
    entry = _file_cache.get(path)
    if entry and entry["mtime"] == stat.st_mtime:
        _record("file_cache", hit=True)
        return {"cached": True, "content": entry["content"], "path": path}

    # ── 2. SQLite cache (warm from previous session) ──
    path_hash = hashlib.md5(path.encode()).hexdigest()
    try:
        conn = _get_db()
        row = conn.execute(
            "SELECT content, mtime FROM file_cache WHERE path_hash = ?", (path_hash,)
        ).fetchone()
        conn.close()
    except Exception as e:
        warnings.warn(f"ToolRecall: SQLite read failed for {path}: {e}")
        row = None

    if row and row["mtime"] == stat.st_mtime:
        _file_cache.put(path, {"content": row["content"], "mtime": row["mtime"], "size": stat.st_size})
        _record("file_cache", hit=True)
        return {"cached": True, "content": row["content"], "path": path}    # ── 3. Cache miss — read from disk ──
    _record("file_cache", hit=False)

    # Security: Hard limit to prevent OOM on huge files (e.g. logs/binaries)
    # 5MB max ~ 1.2M tokens (exceeds most context windows anyway)
    if stat.st_size > 5 * 1024 * 1024:
        return {"error": f"File exceeds 5MB limit ({stat.st_size / 1024 / 1024:.1f} MB). Refusing to cache or read."}

    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()
    except Exception as e:
        return {"error": str(e)}

    _file_cache.put(path, {"content": content, "mtime": stat.st_mtime, "size": stat.st_size})
    _record_tokens_saved("file_cache", _estimate_tokens(content))

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
        conn = _get_db()
        row = conn.execute(
            "SELECT content, cached_at FROM skill_cache WHERE skill_name = ?",
            (skill_name,)
        ).fetchone()
        conn.close()
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
    _record_tokens_saved("skill_cache", _estimate_tokens(content))

    try:
        conn = _get_db()
        conn.execute("""
            INSERT OR REPLACE INTO skill_cache (skill_name, content, file_count, cached_at)
            VALUES (?, ?, ?, ?)
        """, (skill_name, content, len(skill_files), now))
        conn.commit()
        conn.close()
    except Exception as e:
        warnings.warn(f"ToolRecall: SQLite skill persist failed: {e}")

    return {"cached": False, "content": content, "skill": skill_name, "files": len(skill_files)}


# ─── TERMINAL CACHE (SQLite — output not on disk) ──────────

DEFAULT_CACHEABLE = {
    # System info (very static)
    "hostname": 3600,
    "whoami": 3600,
    "pwd": 3600,
    "uname -a": 3600,
    
    # State-checkers (moderate TTL)
    "uptime": 300,
    "free -h": 300,
    "df -h /": 300,
    "crontab -l": 3600,
}


def _match_terminal(cmd: str, pattern: str) -> bool:
    """Match a command against a cacheable pattern.

    - Multi-word patterns (e.g. "git status") require exact match.
    - Single-word patterns (e.g. "hostname", "whoami") require exact match only.
    - No startswith prefix-matching — prevents false cache of "hostname -I".
    """
    # Normalize whitespace
    cmd_norm = " ".join(cmd.strip().split())
    pattern_norm = " ".join(pattern.strip().split())
    return cmd_norm == pattern_norm


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
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=30)
        return {"output": result.stdout, "exit_code": result.returncode, "cached": False}

    cmd_hash = hashlib.md5(cmd.encode()).hexdigest()
    now = time.time()

    try:
        conn = _get_db()
        row = conn.execute(
            "SELECT output, exit_code, expires_at FROM terminal_cache WHERE command_hash = ?",
            (cmd_hash,)
        ).fetchone()

        if row and row["expires_at"] > now:
            conn.execute("UPDATE terminal_cache SET hits = hits + 1 WHERE command_hash = ?", (cmd_hash,))
            conn.commit()
            conn.close()
            _record("terminal_cache", hit=True)
            return {"output": row["output"], "exit_code": row["exit_code"], "cached": True}
        conn.close()
    except Exception as e:
        warnings.warn(f"ToolRecall: SQLite terminal read failed: {e}")

    _record("terminal_cache", hit=False)
    # Use shlex.split for cacheable commands — avoids shell injection
    # e.g. cached_terminal("git status; rm -rf /") → ["git", "status; rm -rf /"] → fails safely
    try:
        cmd_parts = shlex.split(cmd, posix=_POSIX_MODE)
        result = subprocess.run(cmd_parts, capture_output=True, text=True, timeout=30)
    except Exception:
        # Fallback to shell=True for complex commands
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=30)

    expires = now + (cacheable_ttl or 300)
    try:
        conn = _get_db()
        conn.execute("""
            INSERT OR REPLACE INTO terminal_cache (command_hash, command, output, exit_code, cached_at, expires_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (cmd_hash, cmd, result.stdout, result.returncode, now, expires))
        conn.commit()
        conn.close()
    except Exception as e:
        warnings.warn(f"ToolRecall: SQLite terminal persist failed: {e}")

    _record_tokens_saved("terminal_cache", _estimate_tokens(result.stdout))

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
        result = subprocess.run(f"{path} {args}", shell=True, capture_output=True, text=True, timeout=60)
        return {"output": result.stdout, "exit_code": result.returncode, "cached": False}

    is_cacheable = ext in SCRIPT_CACHEABLE_EXTENSIONS

    if not is_cacheable:
        result = subprocess.run(f"{path} {args}", shell=True, capture_output=True, text=True, timeout=60)
        return {"output": result.stdout, "exit_code": result.returncode, "cached": False}

    path_hash = hashlib.md5(f"{path}:{args}".encode()).hexdigest()
    now = time.time()

    try:
        conn = _get_db()
        row = conn.execute(
            "SELECT output, exit_code, cached_at FROM script_cache WHERE script_hash = ?",
            (path_hash,)
        ).fetchone()
        conn.close()
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
    except Exception:
        result = subprocess.run(f"{path} {args}", shell=True, capture_output=True, text=True, timeout=60)

    try:
        conn = _get_db()
        conn.execute("""
            INSERT OR REPLACE INTO script_cache (script_hash, script_path, args, output, exit_code, cached_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (path_hash, path, args, result.stdout, result.returncode, now))
        conn.commit()
        conn.close()
    except Exception as e:
        warnings.warn(f"ToolRecall: SQLite script persist failed: {e}")

    _record_tokens_saved("script_cache", _estimate_tokens(result.stdout))

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

    code_hash = hashlib.md5(code.encode()).hexdigest()
    now = time.time()

    try:
        conn = _get_db()
        row = conn.execute(
            "SELECT output, exit_code, cached_at FROM code_cache WHERE code_hash = ?",
            (code_hash,)
        ).fetchone()
        conn.close()
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
        conn = _get_db()
        conn.execute("""
            INSERT OR REPLACE INTO code_cache (code_hash, code, output, exit_code, cached_at)
            VALUES (?, ?, ?, ?, ?)
        """, (code_hash, code, result.stdout, result.returncode, now))
        conn.commit()
        conn.close()
    except Exception as e:
        warnings.warn(f"ToolRecall: SQLite code persist failed: {e}")

    _record_tokens_saved("code_cache", _estimate_tokens(result.stdout))

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
        _record_tokens_saved("write_cache", _estimate_tokens(content))
        return {"unchanged": True, "path": path}

    # Content differs — write
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
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
            _record_tokens_saved("patch_cache", _estimate_tokens(new_string))
            return {"unchanged": True, "reason": "already_applied", "path": path}
        # Both present — check position
        idx_old = content.find(old_string)
        idx_new = content.find(new_string)
        if idx_new <= idx_old:
            # new_string appears at or before old_string — already applied
            _record("patch_cache", hit=True)
            _record_tokens_saved("patch_cache", _estimate_tokens(new_string))
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
        request_hash = hashlib.md5(request_str.encode()).hexdigest()
        return {"cached": False, "key": request_hash, "bypassed": True, "server": server, "tool": tool}

    ttl = ttl if ttl is not None else MCP_DEFAULT_TTL
    args_json = _json.dumps(arguments, sort_keys=True) if arguments else "{}"
    request_str = f"{server}://{tool}?{args_json}"
    request_hash = hashlib.md5(request_str.encode()).hexdigest()
    now = time.time()

    try:
        conn = _get_db()
        row = conn.execute(
            "SELECT data, expires_at FROM mcp_cache WHERE request_hash = ?",
            (request_hash,)
        ).fetchone()

        if row and row["expires_at"] > now:
            conn.execute("UPDATE mcp_cache SET hits = hits + 1 WHERE request_hash = ?", (request_hash,))
            conn.commit()
            conn.close()
            _record("mcp_cache", hit=True)
            return {"cached": True, "data": row["data"], "server": server, "tool": tool}

        conn.close()
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
        conn = _get_db()
        conn.execute("""
            INSERT OR REPLACE INTO mcp_cache 
            (request_hash, mcp_server, mcp_tool, arguments, data, cached_at, expires_at) 
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (request_hash, server, tool, args_json, data, now, expires))
        conn.commit(); conn.close()
    except Exception as e:
        warnings.warn(f"ToolRecall: MCP cache store failed: {e}")

    _record_tokens_saved("mcp_cache", _estimate_tokens(data))

def invalidate_mcp_server(server: str):
    """Invalidate all cached items for a specific MCP server."""
    try:
        conn = _get_db()
        conn.execute("DELETE FROM mcp_cache WHERE mcp_server = ?", (server,))
        conn.commit()
        conn.close()
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
        conn = _get_db()
        for row in conn.execute("SELECT * FROM cache_stats"):
            total = row["hits"] + row["misses"]
            stats[row["category"]] = {
                "hits": row["hits"],
                "misses": row["misses"],
                "tokens_intercepted": row["tokens_intercepted"],
                "hit_rate": f"{row['hits']/total*100:.0f}%" if total > 0 else "0%",
            }
        for t in ["file_cache", "skill_cache", "terminal_cache", "script_cache", "code_cache", "mcp_cache"]:
            r = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()
            stats[f"{t}_entries"] = r[0]
        conn.close()
    except Exception as e:
        warnings.warn(f"ToolRecall: SQLite stats failed: {e}")

    stats["memory_file_entries"] = len(_file_cache)
    stats["memory_used_mb"] = round(_file_cache.memory_bytes() / (1024 * 1024), 2)
    stats["memory_max_mb"] = MAX_MEMORY_MB
    with _skill_cache_lock:
        stats["memory_skill_entries"] = len(_skill_cache)

    return stats


def reset_stats():
    """Reset cache statistics counters (hits, misses, tokens_intercepted) without clearing cache entries."""
    conn = _get_db()
    try:
        conn.execute("DELETE FROM cache_stats")
        conn.commit()
    except Exception as e:
        warnings.warn(f"ToolRecall: reset_stats failed: {e}")
    finally:
        conn.close()


def invalidate_all():
    """Clear ALL caches (memory + SQLite)."""
    _file_cache.clear()
    with _skill_cache_lock:
        _skill_cache.clear()

    conn = _get_db()
    try:
        conn.execute("DELETE FROM file_cache")
        conn.execute("DELETE FROM skill_cache")
        conn.execute("DELETE FROM terminal_cache")
        conn.execute("DELETE FROM script_cache")
        conn.execute("DELETE FROM code_cache")
        conn.execute("DELETE FROM mcp_cache")
        conn.commit()
    except Exception as e:
        warnings.warn(f"ToolRecall: SQLite invalidate failed: {e}")
    finally:
        conn.close()


def invalidate_file(path: str):
    """Invalidate a specific file from cache (memory + SQLite)."""
    path = os.path.expanduser(path)
    _file_cache.remove(path)

    h = hashlib.md5(path.encode()).hexdigest()
    conn = _get_db()
    try:
        conn.execute("DELETE FROM file_cache WHERE path_hash = ?", (h,))
        conn.commit()
    except Exception as e:
        warnings.warn(f"ToolRecall: SQLite file invalidate failed: {e}")
    finally:
        conn.close()


_init()

def garbage_collect() -> int:
    """Remove expired cache entries and vacuum database to free disk space."""
    import time
    try:
        conn = _get_db()
        now = time.time()
        c1 = conn.execute("DELETE FROM terminal_cache WHERE expires_at < ?", (now,)).rowcount
        c2 = conn.execute("DELETE FROM mcp_cache WHERE expires_at < ?", (now,)).rowcount
        conn.commit()
        conn.execute("VACUUM")
        conn.close()
        return c1 + c2
    except Exception as e:
        warnings.warn(f"ToolRecall GC failed: {e}")
        return -1

