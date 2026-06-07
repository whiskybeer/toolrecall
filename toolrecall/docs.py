"""ToolRecall Docs -- FTS5 knowledge base.

Full-text search over indexed documents (skills, projects, etc.).
No embedding, no GPU, no API call -- pure SQLite FTS5 + BM25.
"""
import os, time, sqlite3, hashlib, subprocess
from pathlib import Path
from toolrecall.config import load_config

config = load_config()


def _get_db_path():
    return os.path.expanduser(config.get("paths", "knowledge_db", default="~/.toolrecall/knowledge.db"))


def _get_db():
    db_path = _get_db_path()
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path)
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


def docs_get_page(path: str, source: str = "hermes") -> str:
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


def index_all(scan_dirs: list = None, extensions: tuple = None, ignore_dirs: set = None, max_bytes: int = 100000):
    """
    Index all source files.
    Called on first `toolrecall index` or `docs_search()` when DB is missing.
    """
    if scan_dirs is None:
        scan_dirs = config.get("sources", "scan_dirs", default=[str(Path.home())])
    if extensions is None:
        extensions = tuple(config.get("sources", "scan_extensions", default=[".md", ".py", ".js", ".ts", ".tsx", ".html", ".css", ".json", ".sh"]))
    if ignore_dirs is None:
        ignore_dirs = set(config.get("sources", "scan_ignore", default=[".git", "node_modules", ".venv", "dist", "build", "__pycache__"]))

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
    return total