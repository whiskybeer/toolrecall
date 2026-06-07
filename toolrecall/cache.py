"""ToolRecall Cache Core -- SQLite-based tool output cache.

Three cache types:
- file_cache: read_file() -> mtime-based (infinite until file change)
- skill_cache: skill_view() -> mtime-based (infinite until skill update)
- terminal_cache: terminal() -> TTL-based (configurable)
- script_cache: cached_run() -> mtime + TTL (script file execution)
- code_cache: cached_exec() -> content-hash + TTL (Python code strings)

Configuration via toolrecall.toml (see config.py).
"""
import os, sqlite3, time, hashlib
from pathlib import Path
from toolrecall.config import load_config

# Initialize on first import
config = load_config()
DB_PATH = os.path.expanduser(config.cache_db)

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
    tokens_saved INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_file_mtime ON file_cache(mtime);
CREATE INDEX IF NOT EXISTS idx_terminal_expires ON terminal_cache(expires_at);
"""


def _get_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.row_factory = sqlite3.Row
    return conn


def _init():
    conn = _get_db()
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()


def _record(category, hit: bool, tokens_saved: int = 0):
    conn = _get_db()
    if hit:
        conn.execute("""
            INSERT INTO cache_stats (category, hits, misses, tokens_saved)
            VALUES (?, 1, 0, 0)
            ON CONFLICT(category) DO UPDATE SET hits = hits + 1
        """, (category,))
    else:
        conn.execute("""
            INSERT INTO cache_stats (category, hits, misses, tokens_saved)
            VALUES (?, 0, 1, 0)
            ON CONFLICT(category) DO UPDATE SET misses = misses + 1
        """, (category,))
    if tokens_saved:
        conn.execute("""
            INSERT INTO cache_stats (category, hits, misses, tokens_saved)
            VALUES (?, 0, 0, ?)
            ON CONFLICT(category) DO UPDATE SET tokens_saved = tokens_saved + ?
        """, (category, tokens_saved, tokens_saved))
    conn.commit()
    conn.close()


# --- FILE CACHE (read_file) ---

def cached_read(path: str) -> dict:
    """Read file WITH cache (mtime-based)."""
    path = os.path.expanduser(path)
    if not os.path.exists(path):
        return {"error": f"File not found: {path}"}

    stat = os.stat(path)
    path_hash = hashlib.md5(path.encode()).hexdigest()

    conn = _get_db()
    row = conn.execute(
        "SELECT content, mtime FROM file_cache WHERE path_hash = ?", (path_hash,)
    ).fetchone()

    if row and row["mtime"] == stat.st_mtime:
        conn.close()
        _record("file_cache", hit=True, tokens_saved=len(row["content"]) // 4)
        return {"cached": True, "content": row["content"], "path": path}

    _record("file_cache", hit=False)
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()
    except Exception as e:
        return {"error": str(e)}

    conn.execute("""
        INSERT OR REPLACE INTO file_cache (path_hash, path, content, mtime, size, cached_at)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (path_hash, path, content, stat.st_mtime, stat.st_size, time.time()))
    conn.commit()
    conn.close()
    return {"cached": False, "content": content, "path": path}


# --- SKILL CACHE ---

def cached_skill(skill_name: str, skill_dirs: list = None) -> dict:
    """Load skill + linked files WITH cache."""
    if skill_dirs is None:
        skill_dirs = [
            str(Path.home() / ".hermes" / "skills"),
        ]

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

    # All files in the skill directory
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

    conn = _get_db()
    row = conn.execute(
        "SELECT content, cached_at FROM skill_cache WHERE skill_name = ?",
        (skill_name,)
    ).fetchone()

    cache_valid = False
    if row:
        max_mtime = max(f["mtime"] for f in skill_files)
        if row["cached_at"] > max_mtime:
            cache_valid = True

    if cache_valid:
        conn.close()
        _record("skill_cache", hit=True, tokens_saved=len(row["content"]) // 4)
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
    conn.execute("""
        INSERT OR REPLACE INTO skill_cache (skill_name, content, file_count, cached_at)
        VALUES (?, ?, ?, ?)
    """, (skill_name, content, len(skill_files), time.time()))
    conn.commit()
    conn.close()
    return {"cached": False, "content": content, "skill": skill_name, "files": len(skill_files)}


# --- TERMINAL CACHE ---

DEFAULT_CACHEABLE = {
    "git status": 30, "git log --oneline -5": 30, "git branch": 60, "git diff --stat": 30,
    "hostname": 3600, "whoami": 3600, "pwd": 3600, "uname -a": 3600,
    "uptime": 300, "free -h": 300, "df -h /": 300, "ls -la": 60,
    "crontab -l": 3600,
}


def cached_terminal(command: str, ttl: int = None) -> dict:
    """Run command OR return cached result."""
    import subprocess

    cmd = command.strip()
    cacheable_ttl = ttl

    # Determine TTL from Config + Defaults
    all_ttls = dict(DEFAULT_CACHEABLE)
    config_ttls = config.get("cache", "terminal_ttls", default={})
    if isinstance(config_ttls, dict):
        all_ttls.update(config_ttls)

    is_cacheable = False
    for pattern, t in all_ttls.items():
        if cmd == pattern or cmd.startswith(pattern):
            is_cacheable = True
            if cacheable_ttl is None:
                cacheable_ttl = t
            break

    if not is_cacheable:
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=30)
        return {"output": result.stdout, "exit_code": result.returncode, "cached": False}

    cmd_hash = hashlib.md5(cmd.encode()).hexdigest()
    now = time.time()

    conn = _get_db()
    row = conn.execute(
        "SELECT output, exit_code, expires_at FROM terminal_cache WHERE command_hash = ?",
        (cmd_hash,)
    ).fetchone()

    if row and row["expires_at"] > now:
        conn.execute("UPDATE terminal_cache SET hits = hits + 1 WHERE command_hash = ?", (cmd_hash,))
        conn.commit()
        conn.close()
        _record("terminal_cache", hit=True, tokens_saved=len(row["output"]) // 4)
        return {"output": row["output"], "exit_code": row["exit_code"], "cached": True}

    _record("terminal_cache", hit=False)
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=30)

    expires = now + (cacheable_ttl or 300)
    conn.execute("""
        INSERT OR REPLACE INTO terminal_cache (command_hash, command, output, exit_code, cached_at, expires_at)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (cmd_hash, cmd, result.stdout, result.returncode, now, expires))
    conn.commit()
    conn.close()

    return {"output": result.stdout, "exit_code": result.returncode, "cached": False}


# --- SCRIPT CACHE (cached_run) ---

SCRIPT_CACHEABLE_EXTENSIONS = {".py", ".sh", ".bash", ".js", ".ts", ".rs", ".go", ".rb", ".pl"}


def cached_run(script_path: str, args: str = "", ttl: int = 300) -> dict:
    """Run a script file WITH cache (mtime + TTL).

    Caches by: script path + args + file mtime.
    Re-runs if the script file was modified or TTL expired.

    ⚠️ SAFETY: Only use for READ-ONLY / idempotent scripts!
       Scripts that have side effects (write files, API calls, DB writes,
       git push, deployments, etc.) MUST use ttl=0 to disable caching.

    Cache key = hash(script_path + args). If you pass different args,
    it's treated as a different script and cached separately.

    Args:
        script_path: Path to script (.py, .sh, etc.)
        args: Command-line arguments passed to the script
        ttl: Cache TTL in seconds (default 300 = 5 min).
             Set ttl=0 to disable caching for state-changing scripts.

    Returns:
        dict with keys: output, exit_code, cached
    """
    import subprocess

    path = os.path.expanduser(script_path)
    if not os.path.exists(path):
        return {"error": f"Script not found: {path}"}

    stat = os.stat(path)
    ext = os.path.splitext(path)[1].lower()
    # --- ttl=0 means bypass cache entirely ---
    if ttl is not None and ttl <= 0:
        result = subprocess.run(f"{path} {args}", shell=True, capture_output=True, text=True, timeout=60)
        return {"output": result.stdout, "exit_code": result.returncode, "cached": False}

    is_cacheable = ext in SCRIPT_CACHEABLE_EXTENSIONS

    if not is_cacheable:
        result = subprocess.run(f"{path} {args}", shell=True, capture_output=True, text=True, timeout=60)
        return {"output": result.stdout, "exit_code": result.returncode, "cached": False}

    path_hash = hashlib.md5(f"{path}:{args}".encode()).hexdigest()
    now = time.time()

    conn = _get_db()
    row = conn.execute(
        "SELECT output, exit_code, cached_at FROM script_cache WHERE script_hash = ?",
        (path_hash,)
    ).fetchone()

    valid = False
    if row:
        age = now - row["cached_at"]
        if age < ttl and stat.st_mtime < row["cached_at"]:
            valid = True

    if valid:
        conn.close()
        _record("script_cache", hit=True, tokens_saved=len(row["output"]) // 4)
        return {"output": row["output"], "exit_code": row["exit_code"], "cached": True}

    _record("script_cache", hit=False)
    result = subprocess.run(f"{path} {args}", shell=True, capture_output=True, text=True, timeout=60)

    conn.execute("""
        INSERT OR REPLACE INTO script_cache (script_hash, script_path, args, output, exit_code, cached_at)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (path_hash, path, args, result.stdout, result.returncode, now))
    conn.commit()
    conn.close()

    return {"output": result.stdout, "exit_code": result.returncode, "cached": False}


# --- CODE CACHE (cached_exec) ---

def cached_exec(code: str, ttl: int = 120) -> dict:
    """Execute Python code string WITH cache by content hash.

    Same Python code always returns cached result for TTL seconds.
    Useful for idempotent computations: data analysis, report gen, formatting.

    ⚠️ SAFETY: Only use for idempotent code!
       Code that has side effects (file writes, API calls, etc.)
       MUST use ttl=0 to disable caching.

    Cache key = hash(code_string). Same code = same cache entry,
    regardless of what the code does!

    Args:
        code: Python code string (e.g. 'import pandas; print(df.describe())')
        ttl: Cache TTL in seconds (default 120 = 2 min).
             Set ttl=0 to disable caching for state-changing code.

    Returns:
        dict with keys: output, exit_code, cached
    """
    import subprocess

    # --- ttl=0 means bypass cache entirely ---
    if ttl is not None and ttl <= 0:
        result = subprocess.run(
            ["python3", "-c", code],
            capture_output=True, text=True, timeout=30
        )
        return {"output": result.stdout, "exit_code": result.returncode, "cached": False}

    code_hash = hashlib.md5(code.encode()).hexdigest()
    now = time.time()

    conn = _get_db()
    row = conn.execute(
        "SELECT output, exit_code, cached_at FROM code_cache WHERE code_hash = ?",
        (code_hash,)
    ).fetchone()

    valid = False
    if row:
        age = now - row["cached_at"]
        if age < ttl:
            valid = True

    if valid:
        conn.close()
        _record("code_cache", hit=True, tokens_saved=len(row["output"]) // 4)
        return {"output": row["output"], "exit_code": row["exit_code"], "cached": True}

    _record("code_cache", hit=False)
    result = subprocess.run(
        ["python3", "-c", code],
        capture_output=True, text=True, timeout=30
    )

    conn.execute("""
        INSERT OR REPLACE INTO code_cache (code_hash, code, output, exit_code, cached_at)
        VALUES (?, ?, ?, ?, ?)
    """, (code_hash, code, result.stdout, result.returncode, now))
    conn.commit()
    conn.close()

    return {"output": result.stdout, "exit_code": result.returncode, "cached": False}


# --- STATS & ADMIN ---

def get_stats() -> dict:
    """Cache statistics."""
    conn = _get_db()
    stats = {}
    for row in conn.execute("SELECT * FROM cache_stats"):
        total = row["hits"] + row["misses"]
        stats[row["category"]] = {
            "hits": row["hits"],
            "misses": row["misses"],
            "tokens_saved": row["tokens_saved"],
            "hit_rate": f"{row['hits']/total*100:.0f}%" if total > 0 else "0%",
        }
    for t in ["file_cache", "skill_cache", "terminal_cache", "script_cache", "code_cache"]:
        r = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()
        stats[f"{t}_entries"] = r[0]
    conn.close()
    return stats


def invalidate_all():
    """Clear entire cache."""
    conn = _get_db()
    conn.execute("DELETE FROM file_cache")
    conn.execute("DELETE FROM skill_cache")
    conn.execute("DELETE FROM terminal_cache")
    conn.execute("DELETE FROM script_cache")
    conn.execute("DELETE FROM code_cache")
    conn.commit()
    conn.close()


def invalidate_file(path: str):
    """Invalidate cache for one file."""
    path = os.path.expanduser(path)
    h = hashlib.md5(path.encode()).hexdigest()
    conn = _get_db()
    conn.execute("DELETE FROM file_cache WHERE path_hash = ?", (h,))
    conn.commit()
    conn.close()


# --- INIT ---
_init()