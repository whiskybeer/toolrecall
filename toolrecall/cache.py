"""
ToolRecall Cache Core — SQLite-basierter Tool-Output-Cache.

Drei Cache-Typen:
- file_cache: read_file() → mtime-basiert (∞ bis Datei-Änderung)
- skill_cache: skill_view() → mtime-basiert (∞ bis Skill-Update)
- terminal_cache: terminal() → TTL-basiert (konfigurierbar)

Konfiguration via toolrecall.toml (siehe config.py).
"""
import os, sqlite3, time, hashlib
from pathlib import Path
from toolrecall.config import load_config

# Initialisierung beim ersten Import
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


# ─── FILE CACHE (read_file) ────────────────────────────────────────

def cached_read(path: str) -> dict:
    """Liest Datei MIT Cache (mtime-basiert)."""
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


# ─── SKILL CACHE ───────────────────────────────────────────────────

def cached_skill(skill_name: str, skill_dirs: list = None) -> dict:
    """Lädt Skill + Referenzen MIT Cache."""
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

    # Alle Dateien im Skill-Ordner
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


# ─── TERMINAL CACHE ────────────────────────────────────────────────

DEFAULT_CACHEABLE = {
    "git status": 30, "git log --oneline -5": 30, "git branch": 60, "git diff --stat": 30,
    "hostname": 3600, "whoami": 3600, "pwd": 3600, "uname -a": 3600,
    "uptime": 300, "free -h": 300, "df -h /": 300, "ls -la": 60,
    "crontab -l": 3600,
}


def cached_terminal(command: str, ttl: int = None) -> dict:
    """Führt Kommando aus ODER gibt Cache zurück."""
    import subprocess

    cmd = command.strip()
    cacheable_ttl = ttl

    # Aus Config + Defaults TTL bestimmen
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


# ─── STATS & VERWALTUNG ────────────────────────────────────────────

def get_stats() -> dict:
    """Cache-Statistiken."""
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
    for t in ["file_cache", "skill_cache", "terminal_cache"]:
        r = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()
        stats[f"{t}_entries"] = r[0]
    conn.close()
    return stats


def invalidate_all():
    """Kompletten Cache leeren."""
    conn = _get_db()
    conn.execute("DELETE FROM file_cache")
    conn.execute("DELETE FROM skill_cache")
    conn.execute("DELETE FROM terminal_cache")
    conn.commit()
    conn.close()


def invalidate_file(path: str):
    """Cache für eine Datei ungültig machen."""
    path = os.path.expanduser(path)
    h = hashlib.md5(path.encode()).hexdigest()
    conn = _get_db()
    conn.execute("DELETE FROM file_cache WHERE path_hash = ?", (h,))
    conn.commit()
    conn.close()


# ─── INIT ──────────────────────────────────────────────────────────
_init()