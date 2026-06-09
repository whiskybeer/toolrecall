"""ToolRecall Docs -- FTS5 knowledge base.

Full-text search over indexed documents (skills, projects, etc.).
No embedding, no GPU, no API call -- pure SQLite FTS5 + BM25.
"""
import os, time, sqlite3, hashlib, subprocess
from pathlib import Path
from toolrecall.config import load_config

# Lazy config — DO NOT cache at module level. The singleton in config.py
# respects TOOLRECALL_* env vars, but ``from toolrecall.config import load_config``
# followed by module-level ``config = load_config()`` freezes the path at import
# time. Tests that set TOOLRECALL_KNOWLEDGE_DB before their own import still work
# because the config singleton is only initialized once — the FIRST caller's env
# wins. To guarantee isolation, callers MUST set the env var before the module
# is first imported anywhere in the process.
def _get_config():
    return load_config()


def _get_db_path():
    # Environment variable takes priority — allows per-test isolation
    env_path = os.environ.get("TOOLRECALL_KNOWLEDGE_DB")
    if env_path:
        return os.path.expanduser(env_path)
    return os.path.expanduser(_get_config().get("paths", "knowledge_db", default="~/.toolrecall/knowledge.db"))


def _get_db():
    db_path = _get_db_path()
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30.0)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_tables(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS pages (
            source TEXT NOT NULL,
            path TEXT NOT NULL,
            title TEXT,
            content TEXT,
            url TEXT,
            PRIMARY KEY (source, path)
        );
    """)
    conn.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS pages_fts USING fts5(
            source, path, title, content, url,
            tokenize='porter unicode61',
            content='pages',
            content_rowid='rowid'
        );
    """)
    # Trigger to keep FTS5 in sync on INSERT
    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS pages_ai AFTER INSERT ON pages BEGIN
            INSERT INTO pages_fts(rowid, source, path, title, content, url)
            VALUES (new.rowid, new.source, new.path, new.title, new.content, new.url);
        END;
    """)
    # Trigger to keep FTS5 in sync on DELETE
    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS pages_ad AFTER DELETE ON pages BEGIN
            INSERT INTO pages_fts(pages_fts, rowid, source, path, title, content, url)
            VALUES ('delete', old.rowid, old.source, old.path, old.title, old.content, old.url);
        END;
    """)
    # Trigger to keep FTS5 in sync on UPDATE
    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS pages_au AFTER UPDATE ON pages BEGIN
            INSERT INTO pages_fts(pages_fts, rowid, source, path, title, content, url)
            VALUES ('delete', old.rowid, old.source, old.path, old.title, old.content, old.url);
            INSERT INTO pages_fts(rowid, source, path, title, content, url)
            VALUES (new.rowid, new.source, new.path, new.title, new.content, new.url);
        END;
    """)
    conn.commit()


def docs_search(query: str, source: str = None) -> str:
    """
    Full-text search across indexed documents.
    Uses FTS5 MATCH + BM25 ranking.

    Args:
        query: Search term(s) — Porter stemming included
        source: Optional namespace (e.g. 'hermes', 'ki-game')
    """
    import re

    if not os.path.exists(_get_db_path()):
        return "No knowledge database found. Run 'toolrecall index' first."

    # Query sanitize
    q = query[:100].strip()
    q = re.sub(r'["\'\(\)\*\-\?\:]', ' ', q)
    words = [w for w in q.split() if w]
    sanitized = " OR ".join(words) if words else ""
    if not sanitized:
        return "Invalid query."

    conn = _get_db()
    try:
        if source:
            rows = conn.execute("""
                SELECT p.source, p.path, p.title, p.url,
                       snippet(pages_fts, 3, '【', '】', '...', 30) as snippet,
                       bm25(pages_fts, 0.0, 0.0, 10.0, 1.0) as score
                FROM pages_fts f JOIN pages p ON p.path = f.path AND p.source = f.source
                WHERE pages_fts MATCH ? AND p.source = ?
                ORDER BY score ASC LIMIT 10
            """, (sanitized, source)).fetchall()
        else:
            rows = conn.execute("""
                SELECT p.source, p.path, p.title, p.url,
                       snippet(pages_fts, 3, '【', '】', '...', 30) as snippet,
                       bm25(pages_fts, 0.0, 0.0, 10.0, 1.0) as score
                FROM pages_fts f JOIN pages p ON p.path = f.path AND p.source = f.source
                WHERE pages_fts MATCH ?
                ORDER BY score ASC LIMIT 10
            """, (sanitized,)).fetchall()

        if rows:
            res = [f"Found {len(rows)} pages (BM25 weighted):"]
            for r in rows:
                snip = " ".join(r["snippet"].split())
                res.append(f"• **[{r['source']}] {r['title']}** (`{r['path']}`)\n  Match: {snip}")
            conn.close()
            return "\n\n".join(res)

        # Fallback: LIKE-Suche
        like = f"%{query[:50]}%"
        if source:
            rows = conn.execute("""
                SELECT source, path, title, url, SUBSTR(content, 1, 200) as snippet
                FROM pages WHERE source = ? AND (title LIKE ? OR content LIKE ?)
                LIMIT 10
            """, (source, like, like)).fetchall()
        else:
            rows = conn.execute("""
                SELECT source, path, title, url, SUBSTR(content, 1, 200) as snippet
                FROM pages WHERE title LIKE ? OR content LIKE ?
                LIMIT 10
            """, (like, like)).fetchall()
        conn.close()

        if rows:
            res = [f"Found {len(rows)} pages (substring match):"]
            for r in rows:
                res.append(f"• **[{r['source']}] {r['title']}** (`{r['path']}`)\n  Preview: {' '.join(r['snippet'].split())}...")
            return "\n\n".join(res)

        return f"No results for: '{query}'."
    except Exception as e:
        conn.close()
        return f"Search error: {e}"


def docs_get_page(path: str, source: str = "") -> str:
    """Get a single page from the knowledge database."""
    if not os.path.exists(_get_db_path()):
        return "No knowledge database found. Run 'toolrecall index' first."

    conn = _get_db()
    try:
        row = conn.execute(
            "SELECT title, content, url FROM pages WHERE path = ? AND source = ?",
            (path.strip(), source.strip())
        ).fetchone()

        if row:
            conn.close()
            return f"### {row['title']}\\nURL: {row['url']}\\n\\n{row['content']}"

        # Fuzzy
        fuzzy = f"%{path}%"
        row = conn.execute(
            "SELECT path, title, content, url FROM pages WHERE (path LIKE ? OR title LIKE ?) AND source = ? LIMIT 1",
            (fuzzy, fuzzy, source)
        ).fetchone()
        conn.close()

        if row:
            return f"### {row['title']} (matched '{path}' → `{row['path']}`)\nURL: {row['url']}\n\n{row['content']}"

        return f"Page not found: '{path}' in source '{source}'."
    except Exception as e:
        conn.close()
        return f"Error: {e}"


def index_agent_memory(memories_dir: str = None, source: str = "agent-memory") -> int:
    """
    Index agent persistent memory stores (MEMORY.md, USER.md) into the
    knowledge database.

    Each §-delimited entry becomes a separate page, FTS5-searchable.

    Args:
        memories_dir: Path to agent memories/ directory (default: agent_home/memories)
        source: FTS5 source label (default: 'agent-memory')

    Returns number of entries indexed.
    """
    import hashlib, re

    if memories_dir is None:
        agent_home = (
            os.environ.get("AGENT_HOME")
            or os.environ.get("HERMES_HOME")
            or _get_config().get("paths", "agent_home", default=None)
        )
        if agent_home:
            agent_home = os.path.expanduser(agent_home)
        else:
            agent_home = os.path.expanduser("~/.hermes")
        memories_dir = os.path.join(agent_home, "memories")

    conn = _get_db()
    _ensure_tables(conn)
    cursor = conn.cursor()

    memory_files = {
        "MEMORY.md": "Agent memory (environment facts, conventions, lessons)",
        "USER.md": "User profile (preferences, communication style, identity)",
    }

    total = 0
    for fname, description in memory_files.items():
        fpath = os.path.join(memories_dir, fname)
        if not os.path.exists(fpath):
            continue

        with open(fpath, "r", encoding="utf-8", errors="ignore") as f:
            raw = f.read()

        # Split by § delimiter; fallback to blank-line separation
        if "§" in raw:
            entries = [e.strip() for e in raw.split("§") if e.strip()]
        else:
            # No § found — use double-newline as delimiter
            entries = [e.strip() for e in raw.split("\n\n") if e.strip()]
            if not entries:
                # Single block: entire file is one entry
                entries = [raw.strip()]

        for idx, entry in enumerate(entries):
            content_hash = hashlib.md5(entry.encode()).hexdigest()[:12]
            path_key = f"{fname}#{content_hash}"

            title = entry.split("\n")[0][:80].strip()
            if not title:
                title = f"{fname} entry {idx + 1}"

            cursor.execute(
                "INSERT OR REPLACE INTO pages (source, path, title, content, url) VALUES (?, ?, ?, ?, ?)",
                (source, path_key, title, entry,
                 f"file://{fpath}#entry{idx + 1}"),
            )
            total += 1

    conn.commit()
    conn.close()
    return total


def index_hermes_memory(memory_dir: str = None, source: str = "hermes-memory") -> int:
    """Backward-compat wrapper around index_agent_memory.

    DEPRECATED: Use index_agent_memory() instead. The 'hermes-memory'
    source label is kept for existing indexed data.
    """
    import warnings
    warnings.warn(
        "index_hermes_memory is deprecated, use index_agent_memory",
        DeprecationWarning, stacklevel=2,
    )
    return index_agent_memory(memories_dir=memory_dir, source=source)


def index_directory(dir_path: str, source: str = None, extensions: tuple = None,
                    ignore_dirs: set = None, max_bytes: int = 100000) -> int:
    """
    Index all files in a directory into the knowledge database.

    Each file becomes a page, FTS5-searchable via docs_search().

    Args:
        dir_path: Directory to scan (e.g. '~/Documents/Obsidian Vault')
        source: FTS5 source label (default: basename of dir_path)
        extensions: File extensions to include (default: .md)
        ignore_dirs: Directories to skip (default: .git, node_modules, .venv)
        max_bytes: Max file size to index in bytes (default: 100KB)

    Returns number of files indexed.
    """
    import hashlib

    if source is None:
        source = os.path.basename(os.path.expanduser(dir_path))
    if extensions is None:
        extensions = (".md",)
    if ignore_dirs is None:
        ignore_dirs = {".git", "node_modules", ".venv", "dist", "build", "__pycache__"}

    dir_path = os.path.expanduser(dir_path)
    if not os.path.exists(dir_path):
        return 0

    conn = _get_db()
    _ensure_tables(conn)
    cursor = conn.cursor()

    total = 0
    for root, dirs, files in os.walk(dir_path):
        dirs[:] = [d for d in dirs if d not in ignore_dirs]
        for fname in files:
            if not fname.endswith(extensions):
                continue
            full = os.path.join(root, fname)
            rel = os.path.relpath(full, dir_path)
            try:
                with open(full, "r", encoding="utf-8", errors="ignore") as f:
                    content = f.read()
            except Exception:
                continue
            if len(content) > max_bytes:
                content = content[:max_bytes] + "\n...[TRUNCATED]..."

            title = fname
            if fname.endswith(".md"):
                for line in content.split("\n"):
                    if line.startswith("# "):
                        title = line[2:].strip()
                        break

            cursor.execute(
                "INSERT OR REPLACE INTO pages (source, path, title, content, url) VALUES (?, ?, ?, ?, ?)",
                (source, rel, title, content, f"file://{full}"))
            total += 1

    conn.commit()
    conn.close()
    return total


def index_all(scan_dirs: list = None, extensions: tuple = None, ignore_dirs: set = None, max_bytes: int = 100000):
    """
    Index all source files.
    Called on first `toolrecall index` or `docs_search()` when DB is missing.

    Also indexes additional knowledge sources and Hermes memory from config.
    """
    _cfg = _get_config()

    if scan_dirs is None:
        scan_dirs = _cfg.get("sources", "scan_dirs", default=[str(Path.home())])
    if extensions is None:
        extensions = tuple(_cfg.get("sources", "scan_extensions", default=[".md", ".py", ".js", ".ts", ".tsx", ".html", ".css", ".json", ".sh"]))
    if ignore_dirs is None:
        ignore_dirs = set(_cfg.get("sources", "scan_ignore", default=[".git", "node_modules", ".venv", "dist", "build", "__pycache__"]))

    conn = _get_db()
    _ensure_tables(conn)
    cursor = conn.cursor()

    total = 0
    for base in scan_dirs:
        base = os.path.expanduser(base)
        if not os.path.exists(base):
            continue
        source_name = os.path.basename(base) or "root"
        for root, dirs, files in os.walk(base):
            dirs[:] = [d for d in dirs if d not in ignore_dirs]
            for fname in files:
                if not fname.endswith(extensions):
                    continue
                full = os.path.join(root, fname)
                rel = os.path.relpath(full, base)
                try:
                    with open(full, "r", encoding="utf-8", errors="ignore") as f:
                        content = f.read()
                except Exception:
                    continue
                if len(content) > max_bytes:
                    content = content[:max_bytes] + "\n...[TRUNCATED]..."

                title = fname
                if fname.endswith(".md"):
                    for line in content.split("\n"):
                        if line.startswith("# "):
                            title = line[2:].strip()
                            break

                cursor.execute("INSERT OR REPLACE INTO pages (source, path, title, content, url) VALUES (?, ?, ?, ?, ?)",
                               (source_name, rel, title, content, f"local://{source_name}/{rel}"))
                total += 1

    conn.commit()
    conn.close()

    # Additional knowledge sources from config ([[sources.knowledge]])
    _index_config_sources(_cfg)

    # Agent memory from config ([sources.memory])
    memory_cfg = _cfg.get("sources", "memory", default={})
    if isinstance(memory_cfg, dict) and memory_cfg.get("enabled", False):
        try:
            mem_total = index_agent_memory()
            total += mem_total
        except Exception:
            pass

    return total


def _index_config_sources(cfg):
    """Index additional knowledge sources defined in config.toml.[[sources.knowledge]]."""
    raw = cfg.get("sources", "knowledge", default=[])
    if not raw:
        return
    if isinstance(raw, dict):
        raw = [raw]
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        path = entry.get("path", "")
        source = entry.get("source") or os.path.basename(os.path.expanduser(path))
        exts = entry.get("extensions", [".md"])
        if isinstance(exts, str):
            exts = [exts]
        try:
            index_directory(path, source=source, extensions=tuple(exts))
        except Exception:
            pass