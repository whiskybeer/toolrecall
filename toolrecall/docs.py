"""ToolRecall Docs -- FTS5 knowledge base.

Full-text search over indexed documents (skills, projects, etc.).
No embedding, no GPU, no API call -- pure SQLite FTS5 + BM25.
"""

import os
import sqlite3
import sys
import threading
import time
from toolrecall.cache import _hash
from toolrecall.config import load_config


# Lazy config — the Config class in config.py always creates a fresh
# instance from env vars + config files, so this lazy wrapper
# is only needed to defer import time (not for singleton isolation).
# Tests that need isolated DB paths should set TOOLRECALL_KNOWLEDGE_DB
# before the first import.
def _get_config():
    return load_config()


def _get_db_path():
    # Environment variable takes priority — allows per-test isolation
    env_path = os.environ.get("TOOLRECALL_KNOWLEDGE_DB")
    if env_path:
        return os.path.expanduser(env_path)
    return os.path.expanduser(
        _get_config().get("paths", "knowledge_db", default="~/.toolrecall/knowledge.db")
    )


# ── Default index sources ────────────────────────────────────────────────────
# Curated, small, bounded — deliberately NOT $HOME. Indexing the entire home
# directory was the original default: it produced a multi-GB DB full of junk
# sources (3.4 GB for 7 MB of useful content, before compaction). The dirs
# hold the content an agent actually searches: its own memory and skills,
# resolved from the agent home (AGENT_HOME env → [paths].agent_home →
# ~/.hermes fallback) so any agent — not just Hermes — gets a sane default.


def _default_scan_dirs() -> list:
    """Agent-agnostic curated default scan dirs: <agent_home>/memories + /skills."""
    agent_home = _get_config().agent_home
    return [os.path.join(agent_home, "memories"), os.path.join(agent_home, "skills")]


def _get_db():
    db_path = _get_db_path()
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30.0)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.row_factory = sqlite3.Row
    return conn


# ── Index freshness ──────────────────────────────────────────────────────────
# The FTS index is a projection of on-disk/remote sources; without a freshness
# signal it silently rots. last_index epoch lives in the DB itself so every
# consumer (CLI, daemon, tests) sees the same truth without extra state files.

_INDEX_META_TABLE = """
    CREATE TABLE IF NOT EXISTS index_meta (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );
"""


def _ensure_meta_table(conn):
    conn.execute(_INDEX_META_TABLE)


def get_last_index_time() -> float | None:
    """Epoch seconds of the last completed indexing run, or None if never."""
    db_path = _get_db_path()
    if not os.path.exists(db_path):
        return None
    try:
        conn = sqlite3.connect(db_path, timeout=5.0)
        try:
            row = conn.execute("SELECT value FROM index_meta WHERE key='last_index'").fetchone()
        finally:
            conn.close()
        return float(row[0]) if row else None
    except (sqlite3.Error, ValueError):
        return None


def _stamp_last_index(conn: sqlite3.Connection) -> None:
    """Record index completion time. Caller commits."""
    conn.execute(_INDEX_META_TABLE)
    conn.execute(
        "INSERT OR REPLACE INTO index_meta (key, value) VALUES ('last_index', ?)",
        (str(time.time()),),
    )


def get_index_ttl() -> float:
    """Seconds after which the index is considered stale (0 = never refresh)."""
    val = _get_config().get("docs", "index_ttl", default=86400)
    try:
        return float(val or 0)
    except (TypeError, ValueError):
        return 86400.0


def _auto_refresh_sources_configured() -> bool:
    """True when there is anything to index, including the safe default.

    The default scan_dirs is the curated _default_scan_dirs() (agent memory +
    skills — small, bounded), never $HOME. Auto-refresh therefore runs with
    the default too; it is skipped only when sources resolve to an empty
    set (nothing to index).
    """
    cfg = _get_config()
    scan_dirs = cfg.get("sources", "scan_dirs", default=None)
    if scan_dirs is None:
        scan_dirs = _default_scan_dirs()  # explicit None behaves as unconfigured
    if isinstance(scan_dirs, list) and scan_dirs:
        return True
    knowledge = cfg.get("sources", "knowledge", default=[])
    if knowledge:
        return True
    memory = cfg.get("sources", "memory", default={})
    if isinstance(memory, dict) and memory.get("enabled", False):
        return True
    return False


# Single-flight guard: concurrent docs_search calls must not spawn parallel
# index_all() runs against the same DB.
_refresh_lock = threading.Lock()
_refreshing = False


def _maybe_refresh_index_async() -> None:
    """Kick a background reindex if the index is stale. Never blocks, never raises.

    Search always proceeds against the (possibly stale) index — the refresh
    only improves the *next* query. A TTL of 0 disables auto-refresh entirely,
    as does an unconfigured default source set (see _auto_refresh_sources_configured).
    """
    global _refreshing
    ttl = get_index_ttl()
    if ttl <= 0:
        return
    if not _auto_refresh_sources_configured():
        return
    last = get_last_index_time()
    if last is not None and (time.time() - last) < ttl:
        return
    with _refresh_lock:
        if _refreshing:
            return  # a refresh is already in flight
        _refreshing = True
    t = threading.Thread(target=_refresh_worker, daemon=True, name="docs-index-refresh")
    t.start()


def _refresh_worker() -> None:
    global _refreshing
    try:
        index_all()
        _maybe_compact_index()
    except Exception as e:
        # Background refresh is best-effort: a stale index still serves and
        # the next query retries. But the failure must be visible — a fully
        # silent refresh loop hides a broken index for days.
        print(f"⚠️  docs refresh failed (stale index still serving): {e}")
    finally:
        with _refresh_lock:
            _refreshing = False


# ── Index compaction ─────────────────────────────────────────────────────────
# External-content FTS5 accumulates stale term data on every re-index
# (INSERT OR REPLACE fires delete+insert per row; deleted docid data stays in
# pages_fts_data until an explicit 'rebuild'/'optimize'). Long-lived installs
# that re-index daily grow multi-GB for a few MB of content — observed at
# 3.4 GB for 7 MB. Compaction = full FTS rebuild from the content table
# (lossless, content='pages' is the source of truth) + VACUUM.

# Auto-compact fires in the refresh worker when the FTS shadow table exceeds
# both thresholds relative to the content it indexes.
_COMPACT_RATIO = 5.0  # fts bytes must exceed content bytes × 5
_COMPACT_MIN_FTS_MB = 50  # …and exceed 50 MB absolute (skip small DBs)


def get_index_bloat() -> tuple[int, int]:
    """(fts_bytes, content_bytes) for the knowledge index, or (0, 0) if
    undeterminable (no dbstat vtab, no DB, empty content)."""
    db_path = _get_db_path()
    if not os.path.exists(db_path):
        return (0, 0)
    try:
        conn = sqlite3.connect(db_path, timeout=10.0)
        try:
            fts = conn.execute(
                "SELECT COALESCE(SUM(pgsize), 0) FROM dbstat WHERE name LIKE 'pages_fts%'"
            ).fetchone()[0]
            content = conn.execute(
                "SELECT COALESCE(SUM(LENGTH(content) + LENGTH(title) + LENGTH(path) + LENGTH(url)), 0) FROM pages"
            ).fetchone()[0]
        finally:
            conn.close()
        return (int(fts), int(content))
    except sqlite3.Error:
        return (0, 0)


def compact_knowledge_db() -> dict:
    """Rebuild the FTS index from the content table and VACUUM the DB.

    Lossless: the external-content table ('content=pages') is regenerated
    purely from `pages`; no document text is touched. Blocks the DB for the
    duration (rebuild re-tokenizes; VACUUM rewrites the file), so callers
    should run it off the query path — the refresh worker does.

    Returns {"before", "after", "reclaimed"} in bytes.
    """
    db_path = _get_db_path()
    before = os.path.getsize(db_path) if os.path.exists(db_path) else 0

    conn = sqlite3.connect(db_path, timeout=60.0)
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("INSERT INTO pages_fts(pages_fts) VALUES('rebuild')")
        conn.commit()
    finally:
        conn.close()

    # VACUUM cannot run inside a transaction.
    conn = sqlite3.connect(db_path, isolation_level=None, timeout=60.0)
    try:
        conn.execute("VACUUM")
    finally:
        conn.close()

    after = os.path.getsize(db_path)
    return {"before": before, "after": after, "reclaimed": before - after}


def _maybe_compact_index() -> None:
    """Best-effort auto-compaction after a background re-index. Never raises."""
    try:
        fts, content = get_index_bloat()
        if fts <= 0 or content <= 0:
            return
        if fts > _COMPACT_MIN_FTS_MB * 1024 * 1024 and fts > _COMPACT_RATIO * content:
            result = compact_knowledge_db()
            print(
                f"✅ docs index compacted: {result['before'] / 1e6:.0f} MB → "
                f"{result['after'] / 1e6:.0f} MB"
            )
    except Exception as e:
        print(f"⚠️  docs index compaction skipped: {e}")


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


def docs_search(query: str, source: str | None = None, _retried: bool = False) -> str:
    """
    Full-text search across indexed documents.
    Uses FTS5 MATCH + BM25 ranking.

    Args:
        query: Search term(s) — Porter stemming included
        source: Optional namespace (e.g. 'hermes', 'my-project')
    """
    import re

    if not os.path.exists(_get_db_path()):
        return "No knowledge database found. Run 'toolrecall index' first."

    # Self-healing: if the index is older than [docs].index_ttl, refresh in a
    # background thread. This query still answers from the current index.
    _maybe_refresh_index_async()

    # Query sanitize
    q = query[:100].strip()
    q = re.sub(r'["\'\(\)\*\-\?\:]', " ", q)
    words = [w for w in q.split() if w]
    sanitized = " OR ".join(words) if words else ""
    if not sanitized:
        return "Invalid query."

    conn = _get_db()
    try:
        if source:
            rows = conn.execute(
                """
                SELECT p.source, p.path, p.title, p.url,
                       snippet(pages_fts, 3, '【', '】', '...', 30) as snippet,
                       bm25(pages_fts, 0.0, 0.0, 10.0, 1.0) as score
                FROM pages_fts f JOIN pages p ON p.path = f.path AND p.source = f.source
                WHERE pages_fts MATCH ? AND p.source = ?
                ORDER BY score ASC LIMIT 10
            """,
                (sanitized, source),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT p.source, p.path, p.title, p.url,
                       snippet(pages_fts, 3, '【', '】', '...', 30) as snippet,
                       bm25(pages_fts, 0.0, 0.0, 10.0, 1.0) as score
                FROM pages_fts f JOIN pages p ON p.path = f.path AND p.source = f.source
                WHERE pages_fts MATCH ?
                ORDER BY score ASC LIMIT 10
            """,
                (sanitized,),
            ).fetchall()

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
            rows = conn.execute(
                """
                SELECT source, path, title, url, SUBSTR(content, 1, 200) as snippet
                FROM pages WHERE source = ? AND (title LIKE ? OR content LIKE ?)
                LIMIT 10
            """,
                (source, like, like),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT source, path, title, url, SUBSTR(content, 1, 200) as snippet
                FROM pages WHERE title LIKE ? OR content LIKE ?
                LIMIT 10
            """,
                (like, like),
            ).fetchall()
        conn.close()

        if rows:
            res = [f"Found {len(rows)} pages (substring match):"]
            for r in rows:
                res.append(
                    f"• **[{r['source']}] {r['title']}** (`{r['path']}`)\n  Preview: {' '.join(r['snippet'].split())}..."
                )
            return "\n\n".join(res)

        return f"No results for: '{query}'."
    except Exception as e:
        conn.close()
        estr = str(e)
        if "malformed" in estr.lower() and not _retried:
            if _rebuild_fts_index():
                return docs_search(query, source=source, _retried=True)
            return (
                f"Search error: {e} — FTS index rebuild failed "
                f"(see daemon log). Run 'toolrecall index-compact' manually."
            )
        return f"Search error: {e}"


def _rebuild_fts_index() -> bool:
    """Rebuild the FTS index from the content table; True on success.

    Self-heal for 'database disk image is malformed' on FTS queries. The
    previous implementation swallowed rebuild failures with `except: pass`,
    so a rebuild that could not run (e.g. the daemon itself or another
    process holding an open connection / WAL lock) silently no-oped and the
    caller retried into the same malformed error forever.

    Recovery order:
    1. WAL checkpoint (TRUNCATE) — merges -wal content into the main DB so
       the rebuild sees a consistent image.
    2. FTS rebuild command.
    Failures are logged to stderr instead of being swallowed.
    """
    db_path = _get_db_path()
    try:
        # Step 1: checkpoint WAL so the rebuild reads a consistent image.
        # A concurrent reader holding the WAL can make this fail — that is
        # fine, the rebuild attempt below still tries.
        try:
            ck = sqlite3.connect(db_path, timeout=10.0)
            ck.execute("PRAGMA wal_checkpoint(TRUNCATE);")
            ck.close()
        except Exception as e:
            print(
                f"[toolrecall] FTS self-heal: WAL checkpoint failed ({e}); "
                f"attempting rebuild anyway",
                file=sys.stderr,
                flush=True,
            )

        # Step 2: rebuild. The rebuild connection must NOT be one of the daemon's
        # pooled/long-lived connections — fresh handle, generous timeout.
        rebuild = sqlite3.connect(db_path, timeout=60.0)
        try:
            rebuild.execute("INSERT INTO pages_fts(pages_fts) VALUES('rebuild')")
            rebuild.commit()
        finally:
            rebuild.close()
        return True
    except Exception as e:
        print(
            f"[toolrecall] FTS self-heal: index rebuild FAILED: {e}",
            file=sys.stderr,
            flush=True,
        )
        return False


def docs_get_page(path: str, source: str = "", _retried: bool = False) -> str:
    """Get a single page from the knowledge database.

    ``_retried`` guards the malformed-index self-heal: one rebuild + one
    retry, never a recursion loop.
    """
    if not os.path.exists(_get_db_path()):
        return "No knowledge database found. Run 'toolrecall index' first."

    conn = _get_db()
    try:
        row = conn.execute(
            "SELECT title, content, url FROM pages WHERE path = ? AND source = ?",
            (path.strip(), source.strip()),
        ).fetchone()

        if row:
            conn.close()
            return f"### {row['title']}\nURL: {row['url']}\n\n{row['content']}"

        # Fuzzy
        fuzzy = f"%{path}%"
        row = conn.execute(
            "SELECT path, title, content, url FROM pages WHERE (path LIKE ? OR title LIKE ?) AND source = ? LIMIT 1",
            (fuzzy, fuzzy, source),
        ).fetchone()
        conn.close()

        if row:
            return f"### {row['title']} (matched '{path}' → `{row['path']}`)\nURL: {row['url']}\n\n{row['content']}"

        return f"Page not found: '{path}' in source '{source}'."
    except Exception as e:
        conn.close()
        estr = str(e)
        if "malformed" in estr.lower() and not _retried:
            if _rebuild_fts_index():
                return docs_get_page(path, source, _retried=True)
            return (
                f"Error: {e} — FTS index rebuild failed "
                f"(see daemon log). Run 'toolrecall index-compact' manually."
            )
        return f"Error: {e}"


def index_agent_memory(memories_dir: str | None = None, source: str = "agent-memory") -> int:
    """
    Index agent persistent memory stores (MEMORY.md, USER.md) into the
    knowledge database.

    Each §-delimited entry becomes a separate page, FTS5-searchable.

    Args:
        memories_dir: Path to agent memories/ directory (default: agent_home/memories)
        source: FTS5 source label (default: 'agent-memory')

    Returns number of entries indexed.
    """

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
            content_hash = _hash(entry)[:12]
            path_key = f"{fname}#{content_hash}"

            title = entry.split("\n")[0][:80].strip()
            if not title:
                title = f"{fname} entry {idx + 1}"

            cursor.execute(
                "INSERT OR REPLACE INTO pages (source, path, title, content, url) VALUES (?, ?, ?, ?, ?)",
                (source, path_key, title, entry, f"file://{fpath}#entry{idx + 1}"),
            )
            total += 1

    conn.commit()
    conn.close()
    return total


def index_directory(
    dir_path: str,
    source: str | None = None,
    extensions: tuple | None = None,
    ignore_dirs: set | None = None,
    max_bytes: int = 100000,
) -> int:
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
            except OSError as e:
                # Unreadable file (perms/race): skip this page but keep the
                # walk alive — a failure to read one doc must not abort the
                # whole refresh. Logged so it is never fully silent.
                print(f"⚠️  docs_refresh: skipping unreadable {rel}: {e}")
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
                (source, rel, title, content, f"file://{full}"),
            )
            total += 1

    _stamp_last_index(conn)
    conn.commit()
    conn.close()
    return total


def index_all(
    scan_dirs: list | None = None,
    extensions: tuple | None = None,
    ignore_dirs: set | None = None,
    max_bytes: int = 100000,
):
    """
    Index all source files.
    Called on first `toolrecall index` or `docs_search()` when DB is missing.

    Also indexes additional knowledge sources and Hermes memory from config.
    """
    _cfg = _get_config()

    if scan_dirs is None:
        scan_dirs = _cfg.get("sources", "scan_dirs", default=None)
    if scan_dirs is None:
        scan_dirs = _default_scan_dirs()
    if extensions is None:
        extensions = tuple(
            _cfg.get(
                "sources",
                "scan_extensions",
                default=[".md", ".py", ".js", ".ts", ".tsx", ".html", ".css", ".json", ".sh"],
            )
        )
    if ignore_dirs is None:
        ignore_dirs = set(
            _cfg.get(
                "sources",
                "scan_ignore",
                default=[".git", "node_modules", ".venv", "dist", "build", "__pycache__"],
            )
        )

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

                cursor.execute(
                    "INSERT OR REPLACE INTO pages (source, path, title, content, url) VALUES (?, ?, ?, ?, ?)",
                    (source_name, rel, title, content, f"local://{source_name}/{rel}"),
                )
                total += 1

    _stamp_last_index(conn)
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
