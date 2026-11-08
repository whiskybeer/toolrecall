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
import re
import json
import sqlite3
import time
import logging
from typing import Any, Callable
import warnings
from datetime import datetime
from threading import Lock
import threading
from collections import OrderedDict
from toolrecall.config import load_config
from toolrecall._db import _db
from toolrecall._db import _init as _db_init
from toolrecall._db import _hash

log = logging.getLogger("toolrecall.cache")

# Agent-facing tools whose calls are counted. Deliberately excludes the
# file/shim commands: their volume is a different question, and mixing them in
# would hide the answer to "does anyone use the knowledge base / MCP surface".
TOOL_USAGE_TRACKED = frozenset(
    ("docs_search", "docs_get_page", "mcp_call", "mcp_list_servers", "cache_status")
)


def record_tool_call(tool: str, ok: bool = True) -> None:
    """Count one agent-facing tool call (and its outcome).

    Why the daemon and not the bridge: the daemon sees every call exactly once,
    with no second DB writer and no extra round trip per tool call.

    Failures are counted as failures rather than skipped: a tool that is called
    and always errors must not look the same as a tool nobody calls.

    Never raises — but never silent either. A counter write that cannot land is
    logged, because "the counter is broken" and "nobody uses the tool" must not
    look identical; that confusion is exactly what let this surface sit
    unmeasured (2026-09-30: docs_search had been advertised for months and had
    never been called once).
    """
    if tool not in TOOL_USAGE_TRACKED:
        return
    try:
        now = time.time()
        with _db() as conn:
            conn.execute(
                "INSERT INTO tool_usage (tool, calls, errors, first_at, last_at)"
                " VALUES (?, 1, ?, ?, ?)"
                " ON CONFLICT(tool) DO UPDATE SET"
                "   calls  = calls + 1,"
                "   errors = errors + excluded.errors,"
                "   last_at = excluded.last_at",
                (tool, 0 if ok else 1, now, now),
            )
    except Exception as e:
        log.warning("tool usage counter write failed for %s: %s", tool, e)


def tool_usage_counts() -> list[dict]:
    """Per-tool call counts, most-called first; empty list when unreadable."""
    try:
        with _db() as conn:
            rows = conn.execute(
                "SELECT tool, calls, errors, first_at, last_at FROM tool_usage"
                " ORDER BY calls DESC, tool"
            ).fetchall()
    except Exception as e:
        log.warning("tool usage counter read failed: %s", e)
        return []
    return [
        {
            "tool": r[0],
            "calls": int(r[1] or 0),
            "errors": int(r[2] or 0),
            "first_at": r[3],
            "last_at": r[4],
        }
        for r in rows
    ]


# Re-export for test compatibility: toolrecall.cache._init() calls _db_init with SCHEMA
def _init():
    _db_init(schema=SCHEMA)


# DB integrity check: run every N seconds to catch corruption early.
# Reset on each successful check so a single bad access doesn't cascade.
_LAST_INTEGRITY_CHECK = 0.0
_INTEGRITY_CHECK_INTERVAL = 600  # 10 minutes
_INTEGRITY_CHECK_IN_PROGRESS = False  # only one background check at a time

# Access-log retention. The prune used to run on EVERY read with a path, as
# `DELETE ... WHERE id NOT IN (SELECT id FROM access_log ORDER BY cached_at DESC
# LIMIT 50000)` — that subquery sorts the whole 50k-row table per read (measured
# 45.3 ms for the subquery alone, 86.4 ms per read on a 1.1 GB live DB), which
# made a cache HIT as expensive as a cold read. It now runs at most once per
# interval and prunes by primary key (no sort). id order == insertion order ==
# cached_at order, so the newest N rows are kept exactly as before.
_LAST_ACCESS_PRUNE = 0.0
_ACCESS_PRUNE_INTERVAL = 60.0  # seconds between prunes
_ACCESS_LOG_KEEP = 50000  # rows to retain

# GC's VACUUM is gated, never unconditional. It rewrites the entire file while
# holding the write lock, and its cost scales with FILE size rather than with what
# it reclaims (measured on the live DB: 2,668 ms for a 98 MB file, ≈27 ms/MB). Run
# every 4 h regardless, that is a multi-second freeze waiting to coincide with
# traffic — so pay it only when the reclaimable share is worth the stall.
_VACUUM_MIN_FREE_MB = 32  # absolute floor: never VACUUM to reclaim less than this
_VACUUM_MIN_FREE_RATIO = 0.20  # ...nor when free pages are below this share of the file


def _ensure_db_integrity():
    """Schedule the periodic DB integrity check OFF the request path.

    Called from _record() on every cache access, but only schedules work
    every _INTEGRITY_CHECK_INTERVAL seconds. If the check reveals a
    malformed database, recovery is attempted via:
      1. Dumping the DB content to a backup
      2. Deleting and recreating the DB
      3. Re-running schema init

    Why it is off-path (2026-09-25): the check used to run inline here, on the
    caller's thread and on the shared ``_db()`` connection. On a 1.1 GB DB
    ``PRAGMA integrity_check`` measured 15.6-20.0 s, so the unlucky read that
    triggered it froze the whole daemon request loop for that long (live log:
    ``cached_read`` peaks at 30.5 s). It now runs in a daemon thread on its OWN
    read-only connection, which (a) never blocks a caller, (b) never holds the
    shared connection lock — WAL readers do not block writers — and (c) runs at
    most one check at a time.

    This is intentionally conservative — it only acts when the DB itself
    reports corruption, not on transient SQL errors.
    """
    global _LAST_INTEGRITY_CHECK, _INTEGRITY_CHECK_IN_PROGRESS
    now = time.time()
    if now - _LAST_INTEGRITY_CHECK < _INTEGRITY_CHECK_INTERVAL:
        return
    if _INTEGRITY_CHECK_IN_PROGRESS:
        return
    # Only the local sqlite backend has a file we can open read-only; a remote
    # backend (libsql) must not be checked by path, and its recovery path
    # differs. Read it via .get(): on Config, `storage_backend` is a plain
    # string attribute (the same-named *method* lives on another class), so
    # `config.storage_backend()` is a TypeError.
    backend = config.get("storage", "backend", default="sqlite")
    if str(backend or "sqlite").lower() != "sqlite":
        _LAST_INTEGRITY_CHECK = now
        return
    db_path = config.get("storage", "db_path", default="")
    if not db_path:
        return

    _LAST_INTEGRITY_CHECK = now
    _INTEGRITY_CHECK_IN_PROGRESS = True
    threading.Thread(
        target=_run_integrity_check,
        args=(str(db_path),),
        name="toolrecall-db-integrity",
        daemon=True,
    ).start()


def _run_integrity_check(db_path: str):
    """Background worker: PRAGMA integrity_check on a private read-only conn.

    Must not use the shared ``_db()`` connection: the check runs for seconds and
    would hold the singleton lock for its whole duration.
    """
    global _INTEGRITY_CHECK_IN_PROGRESS
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=30.0)
        try:
            row = conn.execute("PRAGMA integrity_check").fetchone()
        finally:
            conn.close()
        if row and row[0] != "ok":
            warnings.warn(
                f"ToolRecall: DB integrity check FAILED: {row[0]}. Attempting recovery..."
            )
            _recover_database(db_path)
    except Exception as e:
        msg = str(e)
        if "malformed" in msg:
            warnings.warn(
                "ToolRecall: DB access triggered corruption detection. Attempting recovery..."
            )
            _recover_database(db_path)
        else:
            warnings.warn(f"ToolRecall: DB integrity check failed: {e}")
    finally:
        _INTEGRITY_CHECK_IN_PROGRESS = False


def _recover_database(db_path: str):
    """Attempt to recover a malformed SQLite database.

    Strategy:
      1. Dump recoverable content via ``.dump`` to a temp file.
      2. Rename corrupt DB to .bak.
      3. Recreate fresh DB.
      4. Re-import dump (catches what's recoverable; corrupt pages are
         silently dropped by .dump).
    """
    import subprocess
    import sqlite3 as _sqlite3

    backup_path = db_path + f".corrupt-{int(time.time())}.bak"
    # Need the module-level config's db_path — resolve relative paths
    if not os.path.isabs(db_path):
        toolrecall_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        db_path = os.path.join(toolrecall_dir, db_path)
    dump_path = db_path + ".dump"

    try:
        # 1. Dump what's recoverable
        subprocess.run(
            ["sqlite3", db_path, ".dump"],
            stdout=open(dump_path, "w"),
            stderr=subprocess.DEVNULL,
            timeout=30,
        )
    except Exception as e:
        warnings.warn(f"ToolRecall: DB dump during recovery failed: {e}")
        # Continue anyway — we'll create a fresh DB

    try:
        # 2. Backup corrupt DB
        if os.path.exists(db_path):
            os.rename(db_path, backup_path)
        # Remove WAL/shm sidecars
        for suf in ("-wal", "-shm", ".dump"):
            sidecar = db_path + suf
            if os.path.exists(sidecar):
                os.remove(sidecar)

        # 3. Recreate (SCHEMA lives in this module)
        conn = _sqlite3.connect(db_path, timeout=30.0)
        conn.executescript(SCHEMA)
        conn.commit()
        conn.close()

        # 4. Re-import dump if available
        dump_sql = ""
        if os.path.exists(dump_path):
            with open(dump_path) as f:
                dump_sql = f.read()
            if dump_sql.strip():
                try:
                    conn2 = _sqlite3.connect(db_path, timeout=30.0)
                    conn2.executescript(dump_sql)
                    conn2.commit()
                    conn2.close()
                    warnings.warn(
                        f"ToolRecall: DB recovery complete. Recovered data from {backup_path}"
                    )
                except Exception as e:
                    warnings.warn(f"ToolRecall: DB dump re-import failed (partial recovery): {e}")

        _LAST_INTEGRITY_CHECK = time.time()
    except Exception as e:
        warnings.warn(f"ToolRecall: DB recovery failed: {e}")


config = load_config()


# _hash, SENSITIVE_FILE_PATTERNS, SENSITIVE_FILE_EXTENSIONS, SENSITIVE_BASENAMES,
# _compile_sensitive_patterns, _is_sensitive_path are imported from toolrecall._db.
# We re-import _is_sensitive_path explicitly below for use within this module.
from toolrecall._db import _is_sensitive_path as _is_sensitive_path  # noqa: E402
from toolrecall._db import _compile_sensitive_patterns as _compile_sensitive_patterns  # noqa: E402
from toolrecall.normalizer import canonical_command, normalize_command  # noqa: E402

# ─── In-memory file cache with LRU ──────────────────────────

MAX_MEMORY_MB = config.get(
    "cache", "max_memory_mb", default=20
)  # Default 20 MB (∼model context window)
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
    hits INTEGER DEFAULT 1,
    client TEXT DEFAULT '',
    ns TEXT DEFAULT ''
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
    stderr TEXT NOT NULL DEFAULT '',
    exit_code INTEGER NOT NULL,
    cwd TEXT DEFAULT '',
    cached_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    hits INTEGER DEFAULT 1,
    hit_streak INTEGER DEFAULT 0
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
    tokens_read_uncacheable INTEGER DEFAULT 0,
    io_tokens_saved INTEGER DEFAULT 0,
    tokens_saved_to_llm INTEGER DEFAULT 0,
    tokens_not_sent_measured INTEGER DEFAULT 0,
    updated_at REAL DEFAULT 0
);
-- Lifetime counters the GC's idle-category reset moved aside. The GC zeroes
-- hits/misses/tokens_read_from_disk/io_tokens_saved for categories idle >24 h
-- (so the status view is not dominated by dead categories); it used to do that
-- by DELETING the totals, which destroyed 2.02 B io_tokens_saved on the live DB
-- (found 2026-10-03). Totals are now banked here and stay readable as
-- live + banked (get_stats lifetime_*). A caller banks exactly the columns it
-- zeroes, so a lifetime sum can never double-count:
--   * the GC resets five for categories idle >24 h (see garbage_collect);
--   * reset_stats moves all seven, including the measured bridge-stub axis
--     and the uncacheable-read denominator, because it zeroes the whole row.
-- tokens_saved_to_llm joined the banked set on 2026-10-22: it is reset by the
-- same statement as io_tokens_saved, so leaving it out made the display claim
-- MORE context savings than IO offload after every reset — ctx > io, which is
-- impossible (context savings are a subset of IO offload; seen live at
-- ctx 74.2M vs io 65.0M).
CREATE TABLE IF NOT EXISTS cache_stats_banked (
    category              TEXT PRIMARY KEY,
    hits                  INTEGER NOT NULL DEFAULT 0,
    misses                INTEGER NOT NULL DEFAULT 0,
    tokens_read_from_disk INTEGER NOT NULL DEFAULT 0,
    io_tokens_saved       INTEGER NOT NULL DEFAULT 0,
    tokens_saved_to_llm   INTEGER NOT NULL DEFAULT 0,
    tokens_read_uncacheable INTEGER NOT NULL DEFAULT 0,
    tokens_not_sent_measured INTEGER NOT NULL DEFAULT 0,
    banked_at             REAL,
    bank_count            INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS access_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    category TEXT NOT NULL,
    path TEXT,
    hit INTEGER NOT NULL,
    tokens INTEGER DEFAULT 0,
    cached_at REAL NOT NULL,
    client TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_access_log_time ON access_log(cached_at DESC);
-- Compacted remainders of access_log windows that reset_stats() moved aside.
-- `toolrecall trend` aggregates access_log at query time and has no other
-- source, so a reset used to blank the trend silently. Per day/category/path
-- counts keep every read and hit counted at a fraction of the rows; the infra
-- filter stays in trend.py (applied to live and banked rows alike), so the
-- two sources remain comparable.
CREATE TABLE IF NOT EXISTS access_log_banked (
    day        TEXT NOT NULL,
    category   TEXT NOT NULL,
    path       TEXT NOT NULL DEFAULT '',
    reads      INTEGER NOT NULL DEFAULT 0,
    hits       INTEGER NOT NULL DEFAULT 0,
    misses     INTEGER NOT NULL DEFAULT 0,
    tokens     INTEGER NOT NULL DEFAULT 0,
    banked_at  REAL,
    bank_count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day, category, path)
);
CREATE TABLE IF NOT EXISTS work_ledger (
    key TEXT PRIMARY KEY,
    metrics_json TEXT NOT NULL,
    eval_time REAL DEFAULT 0,
    attempts INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    ttl REAL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_work_ledger_updated ON work_ledger(updated_at DESC);
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
    hits INTEGER DEFAULT 1,
    hit_streak INTEGER DEFAULT 0,
    etag TEXT
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
hits INTEGER DEFAULT 1,
hit_streak INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_api_expires ON api_cache(expires_at);
CREATE TABLE IF NOT EXISTS recall_cache (
    node_id        TEXT PRIMARY KEY,
    fingerprint    TEXT NOT NULL,
    content        TEXT NOT NULL,
    content_type   TEXT NOT NULL,
    reproducible   INTEGER NOT NULL,
    summary        TEXT DEFAULT '',
    tokens         INTEGER DEFAULT 0,
    cached_at      REAL NOT NULL,
    expires_at     REAL
);
CREATE INDEX IF NOT EXISTS idx_recall_type ON recall_cache(content_type);
-- Agent-facing tool usage. Filled by the daemon's request dispatch for the
-- tools agents actually call; the file/shim commands are deliberately NOT
-- counted (their volume — every read in every agent process — is a different
-- question and would bury this one: "is the knowledge/MCP surface used at all".
CREATE TABLE IF NOT EXISTS tool_usage (
    tool     TEXT PRIMARY KEY,
    calls    INTEGER NOT NULL DEFAULT 0,
    errors   INTEGER NOT NULL DEFAULT 0,
    first_at REAL,
    last_at  REAL
);
"""


def _estimate_tokens(text: str) -> int:
    """Estimate LLM token count: code-heavy ~2 char/token, English ~4.
    Weighted average ~3 char/token — better than len//4 for agent content.
    """
    if not text:
        return 0
    return max(1, len(text) // 3)


def _record(
    category,
    hit: bool,
    tokens_read: int = 0,
    path: str = "",
    io_tokens_saved: int = 0,
    context_tokens: int = 0,
    client: str = "",
    tokens_not_sent_measured: int = 0,
):
    """Track cache statistics + access log.

    - hit=True:  increments hit counter and io_tokens_saved
    - hit=False: increments miss counter
    - tokens_read: (only on disk-read miss) records how many tokens
      were read from disk. NOT set on cache hits.
    - path: optional file path for access logging
    - io_tokens_saved: (only on cache hit) records how many tokens were
      served from cache without disk I/O (disk-read avoidance)
    - context_tokens: (only on cache hit, agent-tool reads) records how
      many tokens were served from cache that would have been sent to
      the LLM context. This is a subset of io_tokens_saved — only counts
      reads that originate from agent tools (read_file, skill_view),
      not internal infrastructure reads (config, cron, cwd files).

    Also triggers periodic DB integrity checks.
    """
    _ensure_db_integrity()
    try:
        with _db() as conn:
            if tokens_not_sent_measured:
                # MEASURED context savings (bridge dedup stubs): wire-level
                # verified — the content was REPLACED by a stub, so these
                # tokens provably never reached the LLM. Separate category
                # so the estimate tiers can never contaminate it.
                conn.execute(
                    """
                    INSERT INTO cache_stats (category, hits, misses, tokens_not_sent_measured, updated_at)
                    VALUES ('mcp_dedup', 0, 0, ?, ?)
                    ON CONFLICT(category) DO UPDATE SET
                        tokens_not_sent_measured = tokens_not_sent_measured + ?,
                        updated_at = ?
                """,
                    (
                        tokens_not_sent_measured,
                        time.time(),
                        tokens_not_sent_measured,
                        time.time(),
                    ),
                )
            if hit:
                conn.execute(
                    """
                    INSERT INTO cache_stats (category, hits, misses, tokens_read_from_disk, io_tokens_saved, tokens_saved_to_llm, updated_at)
                    VALUES (?, 1, 0, 0, 0, 0, ?)
                    ON CONFLICT(category) DO UPDATE SET
                        hits = hits + 1,
                        io_tokens_saved = io_tokens_saved + ?,
                        tokens_saved_to_llm = tokens_saved_to_llm + ?,
                        updated_at = ?
                """,
                    (category, time.time(), io_tokens_saved, context_tokens, time.time()),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO cache_stats (category, hits, misses, tokens_read_from_disk, updated_at)
                    VALUES (?, 0, 1, 0, ?)
                    ON CONFLICT(category) DO UPDATE SET
                        misses = misses + 1,
                        updated_at = ?
                """,
                    (category, time.time(), time.time()),
                )
            if tokens_read:
                conn.execute(
                    """
                    INSERT INTO cache_stats (category, hits, misses, tokens_read_from_disk, updated_at)
                    VALUES (?, 0, 0, ?, ?)
                    ON CONFLICT(category) DO UPDATE SET
                        tokens_read_from_disk = tokens_read_from_disk + ?,
                        updated_at = ?
                """,
                    (category, tokens_read, time.time(), tokens_read, time.time()),
                )
            # Access log — only record entries with a meaningful path
            # (skip noise like terminal, mcp, api, browser cache hits without paths)
            if path:
                conn.execute(
                    """
                    INSERT INTO access_log (category, path, hit, tokens, cached_at, client)
                    VALUES (?, ?, ?, ?, ?, ?)
                """,
                    (
                        category,
                        path,
                        1 if hit else 0,
                        # Stub events (tokens_not_sent_measured) are miss-row
                        # entries whose token count must NOT touch the
                        # disk-read counter — surface them in access_log for
                        # per-day trend only.
                        io_tokens_saved
                        if hit
                        else (tokens_read if tokens_read else tokens_not_sent_measured),
                        time.time(),
                        client,
                    ),
                )
                # Access-log prune: at most once per interval, by primary key.
                # (Was a per-read `NOT IN (SELECT ... ORDER BY cached_at DESC
                # LIMIT 50000)` — a full sort of the 50k-row table on every
                # read; see the constants at the top of this module.)
                global _LAST_ACCESS_PRUNE
                _prune_now = time.time()
                if _prune_now - _LAST_ACCESS_PRUNE >= _ACCESS_PRUNE_INTERVAL:
                    _LAST_ACCESS_PRUNE = _prune_now
                    keep = config.get("cache", "access_log_keep", default=_ACCESS_LOG_KEEP)
                    conn.execute(
                        "DELETE FROM access_log WHERE id <= "
                        "(SELECT coalesce(max(id), 0) FROM access_log) - ?",
                        (keep,),
                    )
    except Exception as e:
        warnings.warn(f"ToolRecall: failed to record stats: {e}")


def _record_tokens_read_from_disk(
    category: str, tokens: int, is_new_file: bool = True, uncacheable: bool = False
):
    """Record tokens read from disk.

    Args:
        category: Cache category name
        tokens: Estimated token count of the content
        is_new_file: True = content was never in cache or mtime changed
                      (counts toward tokens_read_from_disk).
                      False = content was in SQLite but not in-memory
                      (don't double-count — already counted on first read).
        uncacheable: True = ttl=0/never-cache read. These go to the separate
                      tokens_read_uncacheable bucket — NO cache could ever
                      have served them, so counting them in the cacheable
                      denominator produced bogus savings ratios (the
                      2026-09 "2%" artifact).
    """
    if tokens <= 0:
        return
    target_col = "tokens_read_uncacheable" if uncacheable else "tokens_read_from_disk"
    try:
        with _db() as conn:
            if is_new_file:
                conn.execute(
                    f"""
                    INSERT INTO cache_stats (category, hits, misses, {target_col})
                    VALUES (?, 0, 0, ?)
                    ON CONFLICT(category) DO UPDATE SET
                        {target_col} = {target_col} + ?,
                        updated_at = ?
                """,
                    (category, tokens, tokens, time.time()),
                )
            else:
                conn.execute(
                    f"""
                    INSERT INTO cache_stats (category, hits, misses, {target_col})
                    VALUES (?, 0, 0, 0)
                    ON CONFLICT(category) DO UPDATE SET updated_at = ?
                """,
                    (category, time.time()),
                )
    except Exception as e:
        warnings.warn(f"ToolRecall: failed to record tokens read from disk: {e}")


# ─── FILE CACHE (hybrid: in-memory LRU + SQLite) ────────────


def _persist_file_to_sqlite(path: str, content: str, stat_result, ns: str = "", client: str = ""):
    """Write file to SQLite for cross-session persistence.

    Files larger than ~10 MB are excluded from SQLite to keep the DB small.
    They remain in the in-memory LRU only, so they're cached per-session
    but not persisted across daemon restarts.

    A6-7: ``ns`` is the scope-namespace ('' = global). A remote member's
    write lands under its scope hash so it can never shadow the global
    entry; ``client`` records write provenance.
    """
    if ns:
        path_hash = _hash(f"{ns}\x00{path}")
    else:
        path_hash = _hash(path)
    try:
        with _db() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO file_cache (path_hash, path, content, mtime, size, cached_at, ns, client)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
                (
                    path_hash,
                    path,
                    content,
                    stat_result.st_mtime,
                    stat_result.st_size,
                    time.time(),
                    ns,
                    client,
                ),
            )
    except Exception as e:
        warnings.warn(f"ToolRecall: SQLite persist failed for {path}: {e}")


def _resolve_cache_path(path: str) -> str:
    """Absolutize and normalize a caller-supplied path for cache identity.

    Cache keys are derived from the path *string* (mem_key, ``_hash(path)``)
    while the file itself is opened against the process's working directory.
    A relative path therefore had two failure modes at once:

      - it resolved against the DAEMON's cwd, not the caller's, so the bytes
        served were whatever the daemon happened to have; and
      - every caller using the same relative name shared one cache entry and
        one file — reading ``NOTES.md`` in project B returned project A's
        content.

    ``normpath`` (not ``realpath``) keeps the lexical identity stable — two
    spellings of the same path share a key, symlinked paths stay distinct, and
    the security layer's own realpath containment check is unaffected.

    Raises:
        ValueError: the path is relative; the daemon cannot know the caller's
            working directory, so guessing would be wrong in both directions.
    """
    expanded = os.path.expanduser(path)
    if not os.path.isabs(expanded):
        raise ValueError(
            f"Path must be absolute (got {path!r}). Cache entries are keyed on the "
            "path string and the daemon resolves relative paths against its own "
            "working directory, not the caller's."
        )
    return os.path.normpath(expanded)


def _bump_file_hits(path_hash: str | None) -> None:
    """Count one cache serve on the persisted file_cache row.

    The column is what per-file honesty tooling reads (bench/cache_honest.py
    classifies ``hits == 1`` as "read from disk once" vs ``hits > 1`` as
    "re-served"), so it must move — before 2026-10-27 it was stored at 1 and
    never touched again, which made every re-read invisible. Sibling caches
    (terminal/mcp/api) maintain theirs the same way.

    Same-transaction note: callers invoke this right after ``_record()``, so
    the write joins the transaction that already logged the hit — one extra
    UPDATE, no extra commit. Best-effort: a failed bump must not fail the read.
    """
    if not path_hash:
        return
    try:
        with _db() as conn:
            conn.execute(
                "UPDATE file_cache SET hits = hits + 1 WHERE path_hash = ?",
                (path_hash,),
            )
    except Exception as e:
        warnings.warn(f"ToolRecall: file hit-counter update failed: {e}")


def cached_read(
    path: str,
    source: str = "",
    ns: str = "",
    client: str = "",
) -> dict:
    """Read file with hybrid cache.

    Lookup order: in-memory LRU (fast) → SQLite (persistent) → disk.

    Args:
        path: File path to read.
        source: Optional source identifier. When "agent_tool", cache hits
            count toward tokens_saved_to_llm (actual LLM context savings)
            in addition to the general io_tokens_saved (disk-read avoidance).
            Empty string or "internal" = only counts disk-read avoidance.
    """
    try:
        path = _resolve_cache_path(path)
    except ValueError as e:
        return {"error": str(e)}

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
                hint = ' Run "toolrecall init" to create a default config.'
        return {"error": f"File not found: {path}.{hint}"}

    stat = os.stat(path)

    # ── 0. Trust-window TTL resolution ([cache].file_ttls) ──
    # resolve_file_ttl() precedence: exact path → glob (folders/filetypes)
    # → [cache].file_ttl global → -1. Semantics:
    #   -1  always mtime-validate (default; unchanged behavior)
    #    0  never cache this file (serve-through, no persistence)
    #   >0  within N seconds of caching, serve WITHOUT mtime validation
    #       (trust window — skips os.stat comparison; trades freshness
    #       guarantees for latency. Opt-in only.)
    from toolrecall.ttl_policy import resolve_file_ttl

    file_ttl = resolve_file_ttl(path, config.get("cache", default={}))
    trust_window = file_ttl > 0
    never_cache = file_ttl == 0
    now_ts = time.time()

    # ── 1. In-memory cache (fast path) ──
    # A6-7: memory is keyed by ns+path for namespaced (remote) callers so a
    # scope-namespace entry never shadows the global one in the LRU.
    mem_key = f"{ns}\x00{path}" if ns else path
    entry = _file_cache.get(mem_key)
    if (
        entry
        and not never_cache
        and (
            trust_window
            and now_ts - entry["cached_at"] <= file_ttl
            or (
                not trust_window
                and entry["mtime"] == stat.st_mtime
                and entry["size"] == stat.st_size
            )
        )
    ):
        tokens = _estimate_tokens(entry["content"])
        context_tokens = tokens if source == "agent_tool" else 0
        _record(
            "file_cache",
            hit=True,
            path=path,
            io_tokens_saved=tokens,
            context_tokens=context_tokens,
            client=client,
        )
        # LRU key == DB row key for the common local caller (ns == ""); a
        # namespaced caller's own-ns row hashes the same way (A6-7). The rare
        # remote edge (ns entry served from the global row) is accepted: the
        # bump lands on the caller's own row, never on a foreign one.
        _bump_file_hits(_hash(mem_key))
        # lossless: decoded text re-encodes to exactly the raw byte size →
        # an rb-mode serve of this entry is byte-identical to disk. Entries
        # predating the newline="" read (CRLF-stripped) derive False.
        return {
            "cached": True,
            "content": entry["content"],
            "path": path,
            "lossless": len(entry["content"].encode("utf-8")) == entry["size"],
        }

    # ttl=0 for this path: serve-through — skip BOTH cache layers entirely.
    # (The disk-read section below honors never_cache by not persisting.)

    # ── 2. SQLite cache (warm from previous session) ──
    # A6-7: two-step lookup for namespaced callers — own scope-ns first,
    # then the global namespace. Global entries are warmed by local
    # (operator) traffic and remain visible to everyone; a remote member's
    # own writes are found first but can never shadow the global entry.
    if ns:
        path_hash_own = _hash(f"{ns}\x00{path}")
        path_hash_global = _hash(path)
    else:
        path_hash_own = _hash(path)
        path_hash_global = None
    row = None
    matched_hash = None  # which row a cache serve below must credit
    try:
        with _db() as conn:
            row = conn.execute(
                "SELECT content, mtime, size, cached_at FROM file_cache WHERE path_hash = ?",
                (path_hash_own,),
            ).fetchone()
            if row is not None:
                matched_hash = path_hash_own
            elif path_hash_global is not None:
                row = conn.execute(
                    "SELECT content, mtime, size, cached_at FROM file_cache WHERE path_hash = ?",
                    (path_hash_global,),
                ).fetchone()
                if row is not None:
                    matched_hash = path_hash_global
    except Exception as e:
        warnings.warn(f"ToolRecall: SQLite read failed for {path}: {e}")
        row = None

    if (
        row
        and not never_cache
        and (
            trust_window
            and now_ts - row["cached_at"] <= file_ttl
            or (not trust_window and row["mtime"] == stat.st_mtime and row["size"] == stat.st_size)
        )
    ):
        _file_cache.put(
            mem_key,
            {
                "content": row["content"],
                "mtime": row["mtime"],
                "size": stat.st_size,
                "cached_at": row["cached_at"],
            },
        )
        tokens = _estimate_tokens(row["content"])
        context_tokens = tokens if source == "agent_tool" else 0
        _record(
            "file_cache",
            hit=True,
            path=path,
            io_tokens_saved=tokens,
            context_tokens=context_tokens,
            client=client,
        )
        _bump_file_hits(matched_hash)
        # lossless derivation mirrors the memory-hit path: row["size"] holds
        # the raw on-disk byte size recorded at miss time.
        return {
            "cached": True,
            "content": row["content"],
            "path": path,  # ── 2. SQLite hit ──
            "lossless": len(row["content"].encode("utf-8")) == row["size"],
        }
    # Miss accounting only applies to cacheable paths. A ttl=0 (never_cache)
    # file is not cache traffic: it can never produce a hit, so counting its
    # reads as misses would permanently depress the hit rate (the Sep 2026
    # stagnation: ~28% of all reads were never-cacheable session transcripts
    # recorded as misses, capping the global rate at ~71%).
    if not never_cache:
        _record("file_cache", hit=False, path=path, client=client)
    # Count tokens_read_from_disk only for truly new content:
    # - brand new file (never in SQLite) → row is None
    # - file was modified (mtime changed) → row exists but stale
    # - trust-window hit already returned above; reaching here under a
    #   trust window means the window EXPIRED → re-read is authoritative
    # If row exists with SAME mtime → SQLite hit should have caught it above
    _row_exists = row is not None
    _mtime_changed = row is not None and row["mtime"] != stat.st_mtime
    _window_expired = (
        row is not None and bool(trust_window) and now_ts - row["cached_at"] > file_ttl
    )
    _is_new_file = row is None or _mtime_changed or _window_expired

    # ── 3. Cache miss — read from disk ──

    # Security: Hard limit to prevent OOM on huge files (e.g. logs/binaries).
    # Default 5MB ~ 1.2M tokens (exceeds most context windows anyway) —
    # configurable via [cache].max_file_mb for deliberate large-file use.
    if stat.st_size > _max_file_bytes():
        return {
            "error": f"File exceeds {config.get('cache', 'max_file_mb', default=5)}MB limit ({stat.st_size / 1024 / 1024:.1f} MB). Refusing to cache or read."
        }

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
        # Newline-preserving text read: newline="" disables universal-newline
        # translation, so "\r\n" survives into the cached string byte-for-byte
        # (decoded as UTF-8 with errors="ignore"). A universal-newlines read
        # here SILENTLY stripped \r — an rb-mode serve of that text returned
        # byte-different content for CRLF files (caught by the 2026-09-15
        # checksum sweep: raw acb24a12… vs shim 1e19d262…).
        # Source of truth for the lossless flag: raw byte size. Decoded text
        # re-encoded is byte-identical iff len == raw byte count (any dropped
        # \r, replacement char, or BOM-as-space shrinks/changes the count).
        with _real_open(path, "r", encoding="utf-8", errors="ignore", newline="") as f:
            content = f.read()
        lossless = len(content.encode("utf-8")) == stat.st_size
    except Exception as e:
        return {"error": str(e)}

    if never_cache:
        # ttl=0: serve-through — no memory LRU entry, no SQLite persistence.
        # content was just read fresh from disk above (authoritative).
        # Bucket: uncacheable — the cache was never allowed to serve this,
        # so it must NOT sit in the cacheable miss denominator (the 2026-09
        # "2% savings" artifact came from exactly this inflation).
        _record_tokens_read_from_disk(
            "file_cache", _estimate_tokens(content), is_new_file=True, uncacheable=True
        )
        return {"cached": False, "content": content, "path": path}

    _file_cache.put(
        mem_key,
        {
            "content": content,
            "mtime": stat.st_mtime,
            "size": stat.st_size,
            "cached_at": now_ts,
            "lossless": lossless,
        },
    )
    _record_tokens_read_from_disk("file_cache", _estimate_tokens(content), is_new_file=_is_new_file)

    # Large files go to SQLite only if they're "static data"
    # Small files always persist for cross-session reuse
    # ttl=0 paths: serve-through only — nothing enters memory or SQLite.
    if not never_cache and stat.st_size <= _max_file_bytes():
        _persist_file_to_sqlite(path, content, stat, ns=ns, client=client)

    return {"cached": False, "content": content, "path": path}


# ─── SKILL CACHE (SQLite + in-memory) ───────────────────────

_skill_cache: dict[str, dict] = {}
_skill_cache_lock = Lock()


def cached_skill(skill_name: str, skill_dirs: list | None = None) -> dict:
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

    skill_files: list[dict[str, Any]] = []
    for root, dirs, files in os.walk(skill_path):
        for f in files:
            full = os.path.join(root, f)
            st = os.stat(full)
            skill_files.append(
                {
                    "path": full,
                    "rel": os.path.relpath(full, skill_path),
                    "mtime": st.st_mtime,
                    "size": st.st_size,
                }
            )

    if not skill_files:
        return {"error": f"No files found in skill: {skill_name}"}

    max_mtime = max(f["mtime"] for f in skill_files) if skill_files else 0
    with _skill_cache_lock:
        mem = _skill_cache.get(skill_name)
        if mem and mem.get("cached_at", 0) >= max_mtime:
            _record("skill_cache", hit=True)
            return {
                "cached": True,
                "content": mem["content"],
                "skill": skill_name,
                "files": len(skill_files),
            }

    try:
        with _db() as conn:
            row = conn.execute(
                "SELECT content, cached_at FROM skill_cache WHERE skill_name = ?", (skill_name,)
            ).fetchone()
    except Exception as e:
        warnings.warn(f"ToolRecall: SQLite skill read failed: {e}")
        row = None

    if row and row.get("cached_at", 0) >= max_mtime:
        with _skill_cache_lock:
            _skill_cache[skill_name] = {"content": row["content"], "cached_at": row["cached_at"]}
        _record("skill_cache", hit=True)
        return {
            "cached": True,
            "content": row["content"],
            "skill": skill_name,
            "files": len(skill_files),
        }

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
            conn.execute(
                """
                INSERT OR REPLACE INTO skill_cache (skill_name, content, file_count, cached_at)
                VALUES (?, ?, ?, ?)
            """,
                (skill_name, content, len(skill_files), now),
            )
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
    "crontab -l": 300,
}


_SHELL_METACHARS = (";", "&", "|", "`", "$(", ">", "<", "\\n")


def _match_terminal(cmd: str, pattern: str) -> bool:
    """Match a command against a cacheable pattern.

    - Multi-word patterns (e.g. "df -h /") require exact match.
    - Single-word patterns (e.g. "hostname", "uptime") match by prefix.
      This means "uptime -p" matches "uptime".
    - Commands containing shell metacharacters are never cacheable, even when
      their first token is allowlisted. "whoami && ./deploy.sh" has the prefix
      "whoami " but is not the static command the allowlist describes.
    - Safe because DEFAULT_CACHEABLE only contains read-only commands.
      Dangerous commands (rm, sudo, mv, git push, kill) are NOT in the list.
      Users can always bypass with ttl=0.
    """
    cmd_norm = " ".join(cmd.strip().split())
    pattern_norm = " ".join(pattern.strip().split())
    # Compound or redirected commands are not static — never cache them.
    if any(ch in cmd_norm for ch in _SHELL_METACHARS):
        return False
    if " " in pattern_norm:
        # Multi-word pattern: exact match only
        return cmd_norm == pattern_norm
    # Single-word pattern: match by prefix
    # "uptime -p" starts with "uptime " — matches
    # "hostnamectl" matches "hostname"? No: "hostnamectl" != "hostname"
    #   and "hostnamectl" doesn't start with "hostname "
    return cmd_norm == pattern_norm or cmd_norm.startswith(pattern_norm + " ")


_LOG_SHELL_FALLBACK = (
    str(config.get("cache", "log_shell_fallback", default="true") or "true").lower() == "true"
)


def _log_shell_fallback(cmd: str, fallback_type: str = "shell"):
    """Log when shell=True fallback is used (security audit signal)."""
    import logging

    logging.getLogger("toolrecall.cache").warning(
        "shell=True fallback (%s): %.200s", fallback_type, cmd
    )


def _run_terminal_subprocess(cmd: str, exec_cwd: str | None) -> dict:
    """Execute a cacheable terminal command via shlex-split subprocess.

    Returns ``{"output", "stderr", "exit_code"}`` on success or
    ``{"error", "exit_code": -1}`` when the command can't be parsed
    (never falls back to shell=True — command-injection guard).
    """
    import shlex
    import subprocess

    try:
        cmd_parts = shlex.split(cmd, posix=_POSIX_MODE)
        result = subprocess.run(cmd_parts, capture_output=True, text=True, timeout=30, cwd=exec_cwd)
    except (ValueError, OSError, subprocess.TimeoutExpired):
        # SECURITY: shlex.split failed — do NOT fall back to shell=True.
        if _LOG_SHELL_FALLBACK:
            _log_shell_fallback(cmd, "shlex split failed (terminal)")
        return {
            "error": "Command contains unparseable shell syntax. Use a script file instead.",
            "exit_code": -1,
        }
    return {
        "output": result.stdout,
        "stderr": result.stderr,
        "exit_code": result.returncode,
    }


# ─── STALE-WHILE-REVALIDATE + ADAPTIVE TTL SHARED HELPERS ─────────────

# Lazily-created Coalescer for the terminal miss path (opt-in via
# [resilience] coalescing). Created on first use so the config window
# value is read when the feature is actually enabled.
_terminal_coalescer = None

# Single-flight registry for background revalidations (in-process; the
# daemon is the single cache owner). Key: "layer\x00hash".
_revalidate_inflight: set[str] = set()
_revalidate_lock = Lock()


def _max_file_bytes() -> int:
    """File-cache size cap in bytes, from [cache].max_file_mb (default 5).

    Re-read per call so config/env changes and test overrides apply without
    a process restart. Rationale for the 5MB default lives on the config
    property (Config.max_file_mb): a 5MB text file already exceeds every
    mainstream context window, so caching beyond it would book savings that
    were never real context avoided.
    """
    try:
        mb = float(config.get("cache", "max_file_mb", default=5))
    except (TypeError, ValueError):
        mb = 5.0
    if mb <= 0 or mb > 2048:
        mb = 5.0
    return int(mb * 1024 * 1024)


def _swr_window() -> int:
    """Stale-while-revalidate window in seconds (0 = off, the default)."""
    try:
        return int(config.get("cache", "stale_while_revalidate", default=0) or 0)
    except (TypeError, ValueError):
        return 0


def _adaptive_cfg() -> tuple[bool, float, float]:
    """Return (adaptive_ttl, adaptive_factor, adaptive_max_ttl) from config."""
    enabled = bool(config.get("cache", "adaptive_ttl", default=False))

    def _num(key: str, default: float) -> float:
        try:
            return float(config.get("cache", key, default=default))
        except (TypeError, ValueError):
            return default

    return enabled, _num("adaptive_factor", 2.0), _num("adaptive_max_ttl", 86400)


def _next_ttl(base_ttl: int, row) -> int:
    """Effective TTL for a re-store: adaptive stretch by hit streak, else base."""
    adaptive, factor, cap = _adaptive_cfg()
    if not adaptive or row is None:
        return base_ttl
    from toolrecall.ttl_policy import adaptive_ttl

    streak = row["hit_streak"] if "hit_streak" in row.keys() else 0
    return int(adaptive_ttl(base_ttl, streak, factor, cap))


def _maybe_revalidate_terminal(
    cmd_hash: str, cmd: str, exec_cwd: str | None, cacheable_ttl: int
) -> None:
    """Kick off a single-flight background revalidation of a stale terminal entry."""
    key = f"terminal\x00{cmd_hash}"
    with _revalidate_lock:
        if key in _revalidate_inflight:
            return
        _revalidate_inflight.add(key)

    def _run():
        try:
            import shlex
            import subprocess

            try:
                cmd_parts = shlex.split(cmd, posix=_POSIX_MODE)
                result = subprocess.run(
                    cmd_parts, capture_output=True, text=True, timeout=30, cwd=exec_cwd
                )
            except (ValueError, OSError, subprocess.TimeoutExpired):
                return  # stale row stays; next caller retries
            now = time.time()
            try:
                with _db() as conn:
                    old = conn.execute(
                        "SELECT output FROM terminal_cache WHERE command_hash = ?",
                        (cmd_hash,),
                    ).fetchone()
                    changed = old is None or old["output"] != result.stdout
                    streak = (
                        0
                        if changed
                        else max(
                            int(old["hit_streak"] or 0) if "hit_streak" in old.keys() else 0, 0
                        )
                    )
                    adaptive, factor, cap = _adaptive_cfg()
                    from toolrecall.ttl_policy import adaptive_ttl

                    eff_ttl = (
                        adaptive_ttl(cacheable_ttl, streak + 1, factor, cap)
                        if adaptive
                        else cacheable_ttl
                    )
                    conn.execute(
                        """
                        UPDATE terminal_cache
                        SET output = ?, stderr = ?, exit_code = ?, cached_at = ?, expires_at = ?, hit_streak = ?
                        WHERE command_hash = ?
                    """,
                        (
                            result.stdout,
                            result.stderr,
                            result.returncode,
                            now,
                            now + eff_ttl,
                            streak,
                            cmd_hash,
                        ),
                    )
            except Exception as e:
                warnings.warn(f"ToolRecall: SWR revalidate (terminal) failed: {e}")
        finally:
            with _revalidate_lock:
                _revalidate_inflight.discard(key)

    threading.Thread(target=_run, daemon=True, name="tr-swr-terminal").start()


def cached_terminal(command: str, ttl: int | None = None, cwd: str | None = None) -> dict:
    """Run command OR return cached result (TTL-based, SQLite-backed).

    Only commands that match a known-cacheable pattern exactly are cached.
    All other commands execute every time (no cache, no delay).

    Set ttl=0 to bypass the cache entirely (execute every time, no storage).

    Args:
        command: Shell command to run (e.g. 'ls -la').
        ttl: Cache TTL in seconds; None = use per-command/config default,
            0 = bypass the cache entirely.
        cwd: Working directory the command runs in. When supplied, it is
            part of the cache key, so the same command in different
            directories gets a distinct entry. When None, the key stays
            command-only (legacy behavior — used only by callers that
            cannot resolve the true cwd, e.g. the daemon when the client
            did not send one).
    """
    import subprocess
    import shlex

    cmd = " ".join(command.strip().split())
    cacheable_ttl = ttl

    # Resolve the working directory once — it scopes BOTH the cache key and
    # where the command actually runs. When cwd is None, commands run in the
    # current process's directory (legacy) and the key stays command-only.
    exec_cwd = None
    if cwd:
        exec_cwd = os.path.realpath(cwd)

    # ttl=0 means bypass cache entirely — execute fresh, don't store
    if ttl is not None and ttl <= 0:
        try:
            cmd_parts = shlex.split(cmd, posix=_POSIX_MODE)
            result = subprocess.run(
                cmd_parts, capture_output=True, text=True, timeout=30, cwd=exec_cwd
            )
        except (ValueError, OSError, subprocess.TimeoutExpired) as e:
            return {"error": f"Cannot parse command: {e}", "exit_code": -1, "cached": False}
        return {
            "output": result.stdout,
            "stderr": result.stderr,
            "exit_code": result.returncode,
            "cached": False,
        }

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

    # Fuzzy TTL classification (difflib): when no exact/prefix pattern
    # matched, the most-similar known pattern (>= fuzzy_threshold) lends
    # its TTL/cacheability. Classification ONLY — serving always stays
    # under the command's own exact hash key.
    if not is_cacheable and config.get("cache", "fuzzy_ttl_match", default=False):
        from toolrecall.ttl_policy import fuzzy_ttl_for

        try:
            threshold: float = float(config.get("cache", "fuzzy_threshold", default=0.85))
        except (TypeError, ValueError):
            threshold = 0.85
        patterns: dict[str, float] = {k: float(v) for k, v in all_ttls.items()}
        ttl_f: float | None
        matched: bool
        ttl_f, matched = fuzzy_ttl_for(cmd, patterns, threshold)
        if matched and ttl_f:
            is_cacheable = True
            if cacheable_ttl is None:
                cacheable_ttl = int(ttl_f)

    if not is_cacheable:
        # SECURITY: Never use shell=True — command injection risk.
        # Use shlex.split to pass args as a list to subprocess.
        # If shlex fails (complex shell syntax), the caller should use
        # a shell script file via cached_run() instead.
        try:
            cmd_parts = shlex.split(cmd, posix=_POSIX_MODE)
            result = subprocess.run(
                cmd_parts, capture_output=True, text=True, timeout=30, cwd=exec_cwd
            )
        except (ValueError, OSError, subprocess.TimeoutExpired) as e:
            return {"error": f"Cannot parse command: {e}", "exit_code": -1, "cached": False}
        return {
            "output": result.stdout,
            "stderr": result.stderr,
            "exit_code": result.returncode,
            "cached": False,
        }

    # Normalize command for cache key when enabled. canonical_command()
    # (flag-cluster sorting, path/quote canonicalization) is a strict
    # superset of normalize_command() — enabling it makes 'ls -al' and
    # 'ls -la' share a key. Serving stays exact-key, so no cross-key
    # poisoning is possible.
    if config.get("norm", "canonical_commands", default=False):
        cmd_key = canonical_command(cmd)
    else:
        cmd_key = normalize_command(cmd) if config.get("norm", "enabled", default=False) else cmd
    # cwd-scoped key: include the working directory (canonicalized) so the
    # same command in different directories gets a distinct entry instead of
    # sharing a stale/wrong result. When cwd is None the key stays
    # command-only (legacy) — that is the daemon's behavior for clients
    # that did not report their cwd.
    cwd_key = exec_cwd or ""
    key_material = cmd_key
    if cwd_key:
        key_material = f"{cwd_key}\x00{cmd_key}"
    cmd_hash = _hash(key_material)
    now = time.time()

    try:
        with _db() as conn:
            row = conn.execute(
                "SELECT output, stderr, exit_code, expires_at, hit_streak FROM terminal_cache WHERE command_hash = ?",
                (cmd_hash,),
            ).fetchone()
            now = time.time()
            if row is not None:
                from toolrecall import ttl_policy

                tier = ttl_policy.classify_expiry(row["expires_at"], now, _swr_window())
                if tier == ttl_policy.SWR_FRESH:
                    conn.execute(
                        "UPDATE terminal_cache SET hits = hits + 1, hit_streak = hit_streak + 1 WHERE command_hash = ?",
                        (cmd_hash,),
                    )
                    # io_tokens_saved was never passed (2026-09-28): terminal_cache
                    # hits showed io_tokens_saved=0, so the TUI IO-offload column
                    # read 0 despite real replays — same class as the api_cache
                    # (2026-09-21) and mcp_cache (2026-09-24) fixes.
                    _record(
                        "terminal_cache",
                        hit=True,
                        io_tokens_saved=_estimate_tokens(row["output"] or ""),
                    )
                    return {
                        "output": row["output"],
                        "stderr": row["stderr"],
                        "exit_code": row["exit_code"],
                        "cached": True,
                    }
                if tier == ttl_policy.SWR_SERVE_STALE:
                    # SWR: serve stale immediately, revalidate in background
                    _record(
                        "terminal_cache",
                        hit=True,
                        io_tokens_saved=_estimate_tokens(row["output"] or ""),
                    )
                    _maybe_revalidate_terminal(cmd_hash, cmd, exec_cwd, cacheable_ttl or 300)
                    return {
                        "output": row["output"],
                        "stderr": row["stderr"],
                        "exit_code": row["exit_code"],
                        "cached": True,
                        "stale": True,
                    }
                # tier == "expired" → fall through to normal miss
    except Exception as e:
        warnings.warn(f"ToolRecall: SQLite terminal read failed: {e}")

    _record("terminal_cache", hit=False)

    # Request coalescing (opt-in): concurrent identical misses share ONE
    # execution. Waiters get the winner's process result directly — no
    # cache-row dependency needed since the producer returns it.
    if config.get("resilience", "coalescing", default=False):
        from toolrecall.resilience import Coalescer

        global _terminal_coalescer
        if _terminal_coalescer is None:
            _terminal_coalescer = Coalescer(
                window=float(config.get("resilience", "coalescing_window", default=30) or 30)
            )
        coalescer = _terminal_coalescer

        def _execute():
            return _run_terminal_subprocess(cmd, exec_cwd)

        proc = coalescer.run(f"terminal\x00{cmd_hash}", _execute)
    else:
        proc = _run_terminal_subprocess(cmd, exec_cwd)

    if proc.get("error"):
        return {
            "error": proc["error"],
            "exit_code": -1,
            "cached": False,
        }

    base_ttl = cacheable_ttl or 300
    # Adaptive TTL: stretch by the existing row's hit streak when enabled.
    # Content changed vs. the old row → streak resets (handled by reading
    # the old row first; a fresh execution is authoritative).
    eff_ttl = base_ttl
    try:
        with _db() as conn:
            old = conn.execute(
                "SELECT output, hit_streak FROM terminal_cache WHERE command_hash = ?", (cmd_hash,)
            ).fetchone()
            eff_ttl = _next_ttl(base_ttl, old if old and old["output"] == proc["output"] else None)
            conn.execute(
                """
                INSERT OR REPLACE INTO terminal_cache (command_hash, command, output, exit_code, stderr, cwd, cached_at, expires_at, hit_streak)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
                (
                    cmd_hash,
                    cmd,
                    proc["output"],
                    proc["exit_code"],
                    proc["stderr"],
                    cwd_key or "",
                    now,
                    now + eff_ttl,
                    0,  # fresh execution resets streak; continuity via SWR revalidate path
                ),
            )
    except Exception as e:
        warnings.warn(f"ToolRecall: SQLite terminal persist failed: {e}")

    _record_tokens_read_from_disk("terminal_cache", _estimate_tokens(proc["output"]))

    return {
        "output": proc["output"],
        "stderr": proc["stderr"],
        "exit_code": proc["exit_code"],
        "cached": False,
    }


# ─── SHELL WRAPPER STRIPPER (agent-agnostic) ─────────────

# Pattern: every agent (Hermes, Codex, Claude Code, Cursor, Aider)
# wraps its commands in a bash script with infrastructure lines:
#   source snapshot, cd, eval 'real command', export -p, printf cwd, exit
# This module strips those and routes the actual command through
# cached_terminal with allowlist security.

# Lines that are agent infrastructure, not the user's command.
_INFRA_PREFIXES = (
    "set ",
    "source ",
    ". ",
    "builtin cd ",
    "cd ",
    "export ",
    "umask ",
    "printf ",
    "__hermes_ec",
    "__HERMES_CWD__",
    "exit ",
    ">/dev/null",
    "trap ",
    "|| true",
    "|| exit ",
    ")} || true",
    # Hermes cwd marker extraction
    "sed -n",
    # Atomic write prefix (write_file)
    "d=",
    "t=",
    'tmp="$(mktemp',
)


def _strip_shell_wrapper(cmd: str) -> str:
    """Strip known agent shell wrappers from a bash -c command string.

    Strips infrastructure lines (source, cd, export, umask, printf markers,
    exit, trap), then looks for eval '...' patterns to extract the real command.
    Returns the inner command if stripping changed anything, or the original
    if no wrapper was found.
    """
    lines = cmd.strip().split("\n")
    stripped = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        # Skip infrastructure lines
        if any(line.startswith(p) for p in _INFRA_PREFIXES):
            continue
        stripped.append(line)

    if not stripped:
        return ""

    # Join stripped lines (should be just the eval line + exit code capture)
    joined = "\n".join(stripped)

    # Try to extract from eval '...' pattern
    # Patterns: eval 'the command', eval "the command", eval the command
    import re as _re

    m = _re.search(r"""eval\s+['"](?P<inner>[^'"]+)['"]""", joined)
    if m:
        inner = m.group("inner").strip()
        # Unescape single quotes if they were escaped for eval
        inner = inner.replace("'\\''", "'")
        if inner:
            return inner

    # Also check for bare eval (no quotes) — rare but possible
    m2 = _re.search(r"""eval\s+(?P<inner>[\w/.@_-][\w/.@_ -]*)""", joined)
    if m2:
        return m2.group("inner").strip()

    return joined


def cached_shell_exec(wrapped_cmd: str, cwd: str | None = None) -> dict:
    """Execute a wrapped shell command, stripping infrastructure, caching the real command.

    Agent-agnostic: strips known wrapper patterns (Hermes, Codex, Claude Code, etc.)
    and routes the inner command through cached_terminal with its allowlist security.

    The security gate is still the `allowed_terminal_commands` regex allowlist —
    even after stripping, only read-only commands are cached/executed.

    cwd is threaded through to cached_terminal so the cache key is
    directory-scoped (see cached_terminal).
    """
    inner = _strip_shell_wrapper(wrapped_cmd)
    if inner and inner != wrapped_cmd:
        # Wrapper detected — route the inner command through cached_terminal
        return cached_terminal(inner, cwd=cwd)
    # No wrapper detected — pass through as-is (existing behavior)
    return cached_terminal(wrapped_cmd, cwd=cwd)


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
    script_key = (
        f"{path}:{normalize_command(args)}"
        if config.get("norm", "enabled", default=False)
        else f"{path}:{args}"
    )
    path_hash = _hash(script_key)
    now = time.time()

    try:
        with _db() as conn:
            row = conn.execute(
                "SELECT output, exit_code, cached_at FROM script_cache WHERE script_hash = ?",
                (path_hash,),
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
            conn.execute(
                """
                INSERT OR REPLACE INTO script_cache (script_hash, script_path, args, output, exit_code, cached_at)
                VALUES (?, ?, ?, ?, ?, ?)
            """,
                (path_hash, path, args, result.stdout, result.returncode, now),
            )
    except Exception as e:
        warnings.warn(f"ToolRecall: SQLite script persist failed: {e}")

    _record_tokens_read_from_disk("script_cache", _estimate_tokens(result.stdout))

    return {"output": result.stdout, "exit_code": result.returncode, "cached": False}


# ─── CODE CACHE (SQLite + content hash) ─────────────────────


def cached_exec(code: str, ttl: int = 0) -> dict:
    """Execute Python code string WITH cache by content hash (SQLite-backed)."""
    import subprocess

    if ttl is not None and ttl <= 0:
        result = subprocess.run(["python3", "-c", code], capture_output=True, text=True, timeout=30)
        return {"output": result.stdout, "exit_code": result.returncode, "cached": False}

    code_hash = _hash(code)
    now = time.time()

    try:
        with _db() as conn:
            row = conn.execute(
                "SELECT output, exit_code, cached_at FROM code_cache WHERE code_hash = ?",
                (code_hash,),
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
    result = subprocess.run(["python3", "-c", code], capture_output=True, text=True, timeout=30)

    try:
        with _db() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO code_cache (code_hash, code, output, exit_code, cached_at)
                VALUES (?, ?, ?, ?, ?)
            """,
                (code_hash, code, result.stdout, result.returncode, now),
            )
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


def cached_write(path: str, content: str, ns: str = "", client: str = "") -> dict:
    """Write a file, skipping if content is identical to disk.

    Args:
        path: File path to write.
        content: Content to write.
        ns: A6-7 scope-namespace ('' = global). A namespaced write
            invalidates only that namespace's cache entry.
        client: A6-7 write provenance (token-id or ''). Recorded in
            access_log.

    Returns:
        On content-match:  {"unchanged": True, "path": path}
        On new write:      {"cached": False, "path": path, "size": N}
        On error:          {"error": "...", "path": path}
    """
    try:
        path = _resolve_cache_path(path)
    except ValueError as e:
        return {"error": str(e), "path": path}
    mem_key = f"{ns}\x00{path}" if ns else path

    # Create parent directories if needed
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)

    # File doesn't exist yet — always write
    if not os.path.exists(path):
        try:
            with open(path, "w", encoding="utf-8", newline="") as f:
                f.write(content)
            _file_cache.remove(mem_key)
            _record("write_cache", hit=False, path=path, client=client)
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
            with open(path, "w", encoding="utf-8", newline="") as f:
                f.write(content)
            _file_cache.remove(mem_key)
            _record("write_cache", hit=False, path=path, client=client)
            return {"cached": False, "path": path, "size": len(content)}
        except Exception as e:
            return {"error": str(e), "path": path}

    # Read current content and compare.
    # Use the real open() to bypass the OS shim (if installed): a shimmed
    # read here would re-enter cached_read transparently, which can refresh
    # AND persist the disk bytes under the reader's (global) namespace —
    # poisoning cross-namespace isolation (A6-7). Same rationale as the
    # _original_open fallback in cached_read.
    try:
        from toolrecall.shim import _original_open as _real_open
    except ImportError:
        _real_open = open
    # Compare BYTES, not decoded text. A text read applies universal-newline
    # translation, so a CRLF file compared equal to the caller's LF string and
    # the write was silently skipped — the file kept endings nobody wrote. A
    # byte compare also never decodes the file, so a non-UTF-8 file cannot be
    # mis-compared (decoding it with errors="ignore" would hide the difference).
    try:
        with _real_open(path, "rb") as f:
            current_bytes = f.read()
        content_bytes = content.encode("utf-8")
    except Exception as e:
        return {"error": str(e), "path": path}

    if current_bytes == content_bytes:
        _record("write_cache", hit=True, path=path, client=client)
        _record_tokens_read_from_disk("write_cache", _estimate_tokens(content))
        return {"unchanged": True, "path": path}

    # Content differs — write
    try:
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(content)
        _file_cache.remove(
            mem_key
        )  # Invalidate stale cache entry (mtime may not change on fast writes)
        invalidate_file(path, ns=ns)  # Also invalidate SQLite layer (this ns only)
        _record("write_cache", hit=False, path=path, client=client)
        return {"cached": False, "path": path, "size": len(content)}
    except Exception as e:
        return {"error": str(e), "path": path}


def cached_patch(
    path: str, old_string: str, new_string: str, ns: str = "", client: str = ""
) -> dict:
    """Apply a find-and-replace patch, skipping if already applied or missing.

    Three outcomes:
      - old_string gone, new_string present → {"unchanged": True, "reason": "already_applied"}
      - old_string not found                → {"unchanged": True, "reason": "not_found"}
      - patch applied                       → {"cached": False, "path": path, "changes": N}

    "Already applied" requires the target text to be ABSENT and the replacement
    PRESENT — never mere substring containment (a deletion has an empty
    new_string, which is contained in every string).

    ⚠️  Same TOCTOU caveat as cached_write — comparison is read-time only.
    ⚠️  If old_string appears multiple times, only the first is checked.
    A6-7: ns/client — see cached_write.
    """
    try:
        path = _resolve_cache_path(path)
    except ValueError as e:
        return {"error": str(e), "path": path}

    if not os.path.exists(path):
        return {"error": f"File not found: {path}", "path": path}
    # Read current content.
    # Use the real open() to bypass the OS shim (if installed): a shimmed
    # read here would re-enter cached_read transparently and can persist
    # the disk bytes under the global namespace (see cached_write comment;
    # A6-7 namespace-isolation hazard).
    try:
        from toolrecall.shim import _original_open as _real_open
    except ImportError:
        _real_open = open
    # Strict UTF-8 with newline="" — both are required for a lossless patch:
    #   - errors="ignore" silently dropped undecodable bytes, and the UTF-8
    #     rewrite then deleted them from disk (latin-1 "café" came back "caf");
    #   - universal-newline translation turned every \r\n into \n, so the
    #     whole-file rewrite converted a CRLF file to LF.
    # A file we cannot decode exactly is REFUSED, never rewritten.
    try:
        with _real_open(path, "r", encoding="utf-8", errors="strict", newline="") as f:
            content = f.read()
    except UnicodeDecodeError:
        return {
            "error": (
                "File is not valid UTF-8; refusing to patch — rewriting it would "
                "silently drop or corrupt bytes. path=" + path
            ),
            "path": path,
        }
    except Exception as e:
        return {"error": str(e), "path": path}

    # "Already applied" is only provable when the target text is GONE and the
    # replacement is present. Substring/position checks are unsound:
    #   - a deletion (new_string == "") makes ``new_string in content`` always
    #     true, so every deletion reported already_applied and silently left the
    #     file untouched;
    #   - new_string may legitimately occur EARLIER in the file (boilerplate, a
    #     sibling function), which suppressed a real patch.
    if new_string and old_string not in content and new_string in content:
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
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(new_content)
        _file_cache.remove(
            f"{ns}\x00{path}" if ns else path
        )  # Invalidate stale cache entry (mtime may not change on fast writes)
        invalidate_file(path, ns=ns)  # Also invalidate SQLite layer (this ns only)
        _record("patch_cache", hit=False, client=client)
        return {"cached": False, "path": path, "changes": change_count}
    except Exception as e:
        return {"error": str(e), "path": path}


# ─── MCP CACHE (SQLite, TTL-based) ─────────────────────────

MCP_DEFAULT_TTL = config.get("mcp", "default_ttl", default=60)
_POSIX_MODE = os.name != "nt"  # shlex.split: posix=True on Unix, False on Windows


def _mcp_request_key(server: str, tool: str, arguments: dict | None, scope: str = "local") -> str:
    """Canonical MCP cache key derivation — the ONE place that knows how.

    Honors ``[norm] enabled`` (semantic normalization) exactly as the
    transparent cache does. cached_mcp_check and get_mcp_etag both route
    through this so etag validation addresses the same row the check
    reads — never re-derive this formula at a call site.

    ``scope`` selects the namespace. ``"local"`` (default) is the
    historical key space and must stay byte-identical to previous
    releases — existing rows keep hitting. Any other value (e.g.
    ``"shared"``) prefixes the preimage, so a locally cached row and a
    shared row of the SAME server/tool/args can never collide: a shared
    row can never be served as if it were a private one, and vice versa.
    """
    import json as _json

    if config.get("norm", "enabled", default=False) and arguments:
        from toolrecall.normalizer import normalize_tool_args

        args_json = normalize_tool_args(arguments)
    else:
        args_json = _json.dumps(arguments, sort_keys=True) if arguments else "{}"
    preimage = f"{server}://{tool}?{args_json}"
    if scope and scope != "local":
        if not re.fullmatch(r"[a-z0-9_-]{1,32}", scope):
            raise ValueError(f"invalid mcp cache scope: {scope!r}")
        preimage = f"{scope}:{preimage}"
    return _hash(preimage)


def _require_validator() -> bool:
    """True when un-validatable MCP rows must not be served (default: False).

    Implements the Phase C policy finding: a cache row with no validator can
    never be revalidated, so serving it is a guess about freshness. Measured
    on a mutating origin (240 shared requests, 3 agents): clock-based serving
    delivered ~29% wrong results; validate-before-serve delivered 0%.
    """
    return bool(config.get("mcp", "require_validator", default=False))


# Verb tokens that mark a tool as having side effects. Matched against the tool
# name split on non-alphanumerics and camelCase boundaries, so "create_issue",
# "createIssue" and "issues.create" all classify as writes.
_MCP_WRITE_VERBS = frozenset(
    {
        "add",
        "append",
        "approve",
        "archive",
        "assign",
        "cancel",
        "close",
        "comment",
        "commit",
        "create",
        "delete",
        "deploy",
        "disable",
        "dismiss",
        "edit",
        "enable",
        "exec",
        "execute",
        "fork",
        "insert",
        "invite",
        "kill",
        "label",
        "lock",
        "merge",
        "move",
        "navigate",
        "patch",
        "post",
        "publish",
        "push",
        "put",
        "rename",
        "reopen",
        "remove",
        "replace",
        "reset",
        "restart",
        "revert",
        "run",
        "send",
        "set",
        "start",
        "stop",
        "submit",
        "sync",
        "trigger",
        "unlock",
        "update",
        "upload",
        "upsert",
        "write",
    }
)

# A leading read verb settles it: "get_commit", "list_labels" and
# "read_query" contain write-looking nouns but only read.
_MCP_READ_VERBS = frozenset(
    {
        "get",
        "list",
        "search",
        "read",
        "find",
        "fetch",
        "query",
        "describe",
        "show",
        "view",
        "lookup",
        "count",
        "resolve",
        "convert",
        "diff",
        "inspect",
    }
)


def mcp_tool_is_cacheable(tool: str, annotations: dict | None = None) -> bool:
    """True when an MCP tool's result may be served from cache.

    Caching a side-effecting tool is a correctness bug, not a missed saving:
    a repeated ``create_issue`` inside the TTL would return the stored reply
    and never reach the server. The server's own annotations win when present
    (``readOnlyHint`` decides either way; ``destructiveHint`` blocks caching);
    otherwise a leading read verb marks the tool cacheable and any write verb
    in the name marks it uncacheable. Names with neither stay cacheable —
    the pre-existing behaviour, which preserves caching for servers whose
    tools are read-only but named opaquely (e.g. ``docs_search``).

    Residual risk (documented, not hidden): a mutating tool whose name carries
    no write verb AND which the server does not annotate is still classified
    cacheable. A write served from cache is possible in that case, which is
    why the daemon also invalidates a server's cached reads whenever a
    non-cacheable tool is called (see DaemonServer._handle_mcp_call).
    """
    if annotations:
        hint = annotations.get("readOnlyHint")
        if hint is not None:
            return bool(hint)
        if annotations.get("destructiveHint"):
            return False
    import re as _re

    words = _re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", tool or "").lower()
    tokens = [t for t in _re.split(r"[^a-z0-9]+", words) if t]
    if tokens and tokens[0] in _MCP_READ_VERBS:
        return True
    return not (set(tokens) & _MCP_WRITE_VERBS)


def cached_mcp_serve(
    server: str | None = None,
    tool: str | None = None,
    arguments: dict | None = None,
    scope: str = "shared",
    key: str | None = None,
) -> dict:
    """Read-only shared-tier lookup — serves ONLY validator-bearing rows.

    Never writes. Returns ``{"served": True, data, etag, key, age_s}`` when a
    shared row exists AND carries a validator; otherwise
    ``{"served": False, "reason": "miss" | "unvalidated", "key"}``.

    Address the row either by ``key`` (exact row key, e.g. rows written
    through the generic IPC key-value facade) or by ``server``/``tool``/
    ``arguments`` (derived with ``scope``).

    Rationale (Phase C, 2026-09-20): a caller that receives a row without an
    ETag cannot check it against the origin, so the only remaining freshness
    policy is the clock — which delivered ~29% wrong results on a mutating
    origin, including success<->error inversions. Refusing loudly keeps the
    unsafe path unreachable by construction; the caller falls back to its own
    execution, which is always correct.
    """
    if key is not None:
        request_hash = key
    elif server is not None and tool is not None:
        request_hash = _mcp_request_key(server, tool, arguments, scope=scope)
    else:
        return {"served": False, "reason": "bad_request", "key": ""}
    try:
        with _db() as conn:
            row = conn.execute(
                "SELECT data, etag, cached_at, expires_at FROM mcp_cache WHERE request_hash = ?",
                (request_hash,),
            ).fetchone()
    except Exception as e:
        warnings.warn(f"ToolRecall: MCP shared serve failed: {e}")
        return {"served": False, "reason": "error", "key": request_hash}

    if row is None:
        return {"served": False, "reason": "miss", "key": request_hash}
    if not row["etag"]:
        # loud, not silent: the caller must know it got nothing and why
        return {"served": False, "reason": "unvalidated", "key": request_hash}
    return {
        "served": True,
        "data": row["data"],
        "etag": row["etag"],
        "key": request_hash,
        "age_s": max(0.0, time.time() - row["cached_at"]),
        "expired": row["expires_at"] <= time.time(),
    }


def cached_mcp_check(
    server: str,
    tool: str,
    arguments: dict | None = None,
    ttl: int | None = None,
    scope: str = "local",
) -> dict:
    """Check if an MCP tool call result is cached.

    Returns cached data on hit, or a miss indicator with the cache key.
    The caller makes the real MCP call on miss, then calls cached_mcp_store().

    Set ttl=0 to bypass the cache entirely.

    ``scope`` selects the namespace (default ``"local"``, unchanged key space).

    When ``[mcp] require_validator`` is on, a row with no ETag is reported as
    a miss with ``reason="unvalidated"`` instead of being served — a row that
    cannot be revalidated must not be delivered on a clock guess.
    """
    import json as _json

    # ttl=0 means bypass cache
    if ttl is not None and ttl <= 0:
        # Synthetic path: MCP has no filesystem path, but the daily series reads
        # access_log by path — without one, MCP traffic stayed invisible there
        # (0 rows) while cache_stats counted it. `mcp:<server>/<tool>` is
        # stable, greppable, and passes the infra filter (no /.hermes/).
        _record("mcp_cache", hit=False, path=f"mcp:{server}/{tool}")
        args_json = _json.dumps(arguments, sort_keys=True) if arguments else "{}"
        request_str = f"{server}://{tool}?{args_json}"
        request_hash = _hash(request_str)
        return {
            "cached": False,
            "key": request_hash,
            "bypassed": True,
            "server": server,
            "tool": tool,
        }

    ttl = ttl if ttl is not None else MCP_DEFAULT_TTL

    # Normalize MCP arguments for cache key when enabled
    request_hash = _mcp_request_key(server, tool, arguments, scope=scope)
    now = time.time()

    try:
        with _db() as conn:
            row = conn.execute(
                "SELECT data, expires_at, hit_streak, cached_at, etag FROM mcp_cache WHERE request_hash = ?",
                (request_hash,),
            ).fetchone()
            if row is not None:
                if not row["etag"] and _require_validator():
                    # loud refusal: this row can never be revalidated, so
                    # serving it would be a clock-based guess about freshness
                    _record("mcp_cache", hit=False, path=f"mcp:{server}/{tool}")
                    return {
                        "cached": False,
                        "key": request_hash,
                        "server": server,
                        "tool": tool,
                        "reason": "unvalidated",
                    }
                from toolrecall import ttl_policy

                tier = ttl_policy.classify_expiry(row["expires_at"], now, _swr_window())
                # The cached response body is what the server would have
                # produced — replaying it skips the subprocess round-trip
                # entirely. Estimate the avoided IO as tokens (same rule as
                # api_cache: bytes/3 on the stored body).
                io_tokens_saved = _estimate_tokens(row["data"])
                if tier == ttl_policy.SWR_FRESH:
                    conn.execute(
                        "UPDATE mcp_cache SET hits = hits + 1, hit_streak = hit_streak + 1 WHERE request_hash = ?",
                        (request_hash,),
                    )
                    # io_tokens_saved was computed but never passed (2026-09-24):
                    # mcp_cache showed 12 hits with io_tokens_saved=0, so the
                    # TUI IO-offload column read 0 despite real replays —
                    # same class as the api_cache fix. A cached MCP replay
                    # skips the subprocess round-trip entirely: the response
                    # body is what the server would have produced.
                    _record(
                        "mcp_cache",
                        hit=True,
                        io_tokens_saved=io_tokens_saved,
                        path=f"mcp:{server}/{tool}",
                    )
                    return {
                        "cached": True,
                        "data": row["data"],
                        "server": server,
                        "tool": tool,
                        "age_s": max(0.0, now - row["cached_at"]),
                        "etag": row["etag"],
                        "key": request_hash,
                    }
                if tier == ttl_policy.SWR_SERVE_STALE:
                    # SWR: serve stale; revalidation is the CALLER's job
                    # (check has no fetch capability — cached_mcp() handles
                    # it via fetch_fn; daemon-routed callers re-invoke).
                    _record(
                        "mcp_cache",
                        hit=True,
                        io_tokens_saved=io_tokens_saved,
                        path=f"mcp:{server}/{tool}",
                    )
                    return {
                        "cached": True,
                        "data": row["data"],
                        "server": server,
                        "tool": tool,
                        "stale": True,
                        "key": request_hash,
                        "age_s": max(0.0, now - row["cached_at"]),
                        "etag": row["etag"],
                    }
                # tier == "expired" → fall through to miss
    except Exception as e:
        warnings.warn(f"ToolRecall: MCP cache read failed: {e}")

    _record("mcp_cache", False)
    return {"cached": False, "key": request_hash, "server": server, "tool": tool}


def cached_mcp_store(
    request_hash: str,
    server: str,
    tool: str,
    arguments: dict,
    data: str,
    ttl: int | None = None,
    etag: str | None = None,
):
    """Store an MCP tool call result for future cache hits.

    ``etag``: optional HTTP validator (If-None-Match target) captured from
    the live response. Enables conditional revalidation on later refresh
    (get_mcp_etag → fetch_revalidate); NULL stores an un-validated row.
    """
    import json as _json

    ttl = ttl if ttl is not None else MCP_DEFAULT_TTL
    now = time.time()
    args_json = _json.dumps(arguments, sort_keys=True) if arguments else "{}"
    # Adaptive TTL: stretch by the existing row's hit streak (unchanged data
    # keeps the streak; a re-store resets it to 0).
    eff_ttl = ttl
    try:
        with _db() as conn:
            old = conn.execute(
                "SELECT data, hit_streak FROM mcp_cache WHERE request_hash = ?", (request_hash,)
            ).fetchone()
            eff_ttl = _next_ttl(ttl, old if old and old["data"] == data else None)
            conn.execute(
                """
                INSERT OR REPLACE INTO mcp_cache
                (request_hash, mcp_server, mcp_tool, arguments, data, cached_at, expires_at, hit_streak, etag)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
                (request_hash, server, tool, args_json, data, now, now + eff_ttl, 0, etag),
            )
    except Exception as e:
        warnings.warn(f"ToolRecall: MCP cache store failed: {e}")

    _record_tokens_read_from_disk("mcp_cache", _estimate_tokens(data))


def get_mcp_etag(server: str, tool: str, arguments: dict, scope: str = "local") -> str | None:
    """Return the stored HTTP ETag for an mcp_cache row (None if absent).

    Key derivation is shared with cached_mcp_check (via _mcp_request_key),
    so validation always addresses the row the transparent cache reads —
    including ``[norm]`` normalization when enabled. ``scope`` must match the
    namespace the row was stored in.
    """
    request_hash = _mcp_request_key(server, tool, arguments, scope=scope)
    try:
        with _db() as conn:
            row = conn.execute(
                "SELECT etag FROM mcp_cache WHERE request_hash = ?", (request_hash,)
            ).fetchone()
            return row["etag"] if row and row["etag"] else None
    except Exception as e:
        warnings.warn(f"ToolRecall: MCP etag read failed: {e}")
        return None


def refresh_mcp_etag(request_hash: str, etag: str | None, ttl: int | None = None) -> None:
    """Bump an mcp_cache row's freshness after a 304 Not Modified response.

    Content is byte-identical (the ETag proves it) — only cached_at,
    expires_at, and the validator are refreshed; hits/hit_streak are left
    untouched (this is a revalidation, not a hit). A 200 response must NOT
    take this path — the caller stores the new body via cached_mcp_store.
    """
    now = time.time()
    eff_ttl = ttl if ttl is not None else MCP_DEFAULT_TTL
    try:
        with _db() as conn:
            conn.execute(
                "UPDATE mcp_cache SET cached_at = ?, expires_at = ?, etag = ? WHERE request_hash = ?",
                (now, now + eff_ttl, etag, request_hash),
            )
    except Exception as e:
        warnings.warn(f"ToolRecall: MCP etag refresh failed: {e}")


def revalidate_mcp_entry(
    server: str,
    tool: str,
    arguments: dict,
    fetch_fn: Callable[[str], dict | None],
) -> dict:
    """Expired-row revalidation via HTTP validator (If-None-Match).

    Contract:
      - fetch_fn(etag) performs ONE network call; returns a dict with
        ``status`` and optionally ``revalidated`` (304 semantics) or fresh
        content. Returns None on network failure → stale row untouched.
      - 304 (fetch_fn sets revalidated=True) → refresh timestamps via
        refresh_mcp_etag; the row stays byte-identical. Saves the full
        body transfer.
      - 200 with a new etag → replace the row (new body + validator).
      - Anything else / exception → row left as-is; caller decides
        (transparent cache serves stale, adapter bypasses to live call).

    Returns a dict describing what happened: {action, rows, etag}.
    ``action`` ∈ {revalidated, replaced, failed, not_found, no_etag}.
    """
    import json as _json

    request_hash = _mcp_request_key(server, tool, arguments)
    etag = None
    row_exists = False
    try:
        with _db() as conn:
            row = conn.execute(
                "SELECT etag FROM mcp_cache WHERE request_hash = ?", (request_hash,)
            ).fetchone()
            row_exists = row is not None
            etag = row["etag"] if row else None
    except Exception as e:
        warnings.warn(f"ToolRecall: MCP revalidate read failed: {e}")
        return {"action": "failed", "rows": 0}

    if not row_exists:
        return {"action": "not_found", "rows": 0}
    if not etag:
        # Row exists but was never validator-captured: plain refetch would
        # duplicate cached_mcp() — the caller handles that case itself.
        return {"action": "no_etag", "rows": 0}

    try:
        fresh = fetch_fn(etag)
    except Exception as e:
        warnings.warn(f"ToolRecall: MCP revalidate fetch failed: {e}")
        return {"action": "failed", "rows": 0}

    if not isinstance(fresh, dict):
        return {"action": "failed", "rows": 0}

    status = fresh.get("status")
    if fresh.get("revalidated") or status == 304:
        refresh_mcp_etag(request_hash, etag, None)
        return {"action": "revalidated", "rows": 1, "etag": etag}

    if status == 200 and "content" in fresh:
        new_etag = fresh.get("etag")
        serialized = _json.dumps(fresh, default=str, ensure_ascii=False)
        cached_mcp_store(request_hash, server, tool, arguments, serialized, ttl=None, etag=new_etag)
        return {"action": "replaced", "rows": 1, "etag": new_etag}

    return {"action": "failed", "rows": 0}


def _heal_poisoned_mcp_row(request_hash: str) -> None:
    """Delete a cached MCP row that holds an error, not an answer.

    Companion to the daemon's no-errors store rule (2026-09-22): rows
    written before the fix hold transient failures ("Permission denied",
    crashed subprocess) and SWR would keep serving them until expiry even
    after the fix — the next identical call would see the cached error and
    never retry. Deleting on the first error-path call converts the row
    back into an honest miss. Best-effort: a failure here must never mask
    the real result being returned to the caller.
    """
    try:
        with _db() as conn:
            cur = conn.execute("DELETE FROM mcp_cache WHERE request_hash = ?", (request_hash,))
            if cur.rowcount:
                conn.execute(
                    """UPDATE cache_stats
                       SET hits = MAX(hits - 1, 0), updated_at = ?
                       WHERE category = 'mcp_cache' AND hits > 0""",
                    (time.time(),),
                )
    except sqlite3.OperationalError as e:
        # No table (fresh DB) is a legitimate state — nothing to heal.
        # A real failure (locked/corrupt) still deserves its warning.
        if "no such table" not in str(e):
            warnings.warn(f"ToolRecall: mcp_cache poison heal failed: {e}")
    except Exception as e:  # pragma: no cover — heal is best-effort
        warnings.warn(f"ToolRecall: mcp_cache poison clean failed: {e}")


def invalidate_mcp_url(server: str, tool: str, url_substring: str) -> int:
    """Invalidate mcp_cache rows whose stored arguments contain a substring.

    Substring match is case-insensitive against the JSON-serialized
    arguments (the URL lives inside them for fetch-style tools). Returns
    the number of rows deleted — 0 means nothing matched (the caller can
    treat that as "nothing was cached for this site").

    Substring scope keeps this safe from SQL injection (parameterized) and
    from URL-parsing edge cases (a query param IS part of the identity —
    `?page=2` SHOULD be a different row than `?page=1`).
    """
    if not url_substring:
        return 0
    pattern = f"%{url_substring.lower()}%"
    try:
        with _db() as conn:
            cur = conn.execute(
                "DELETE FROM mcp_cache WHERE mcp_server = ? AND mcp_tool = ? AND LOWER(arguments) LIKE ?",
                (server, tool, pattern),
            )
            return cur.rowcount
    except Exception as e:
        warnings.warn(f"ToolRecall: MCP url invalidate failed: {e}")
        return 0


def invalidate_mcp_server(server: str):
    """Invalidate all cached items for a specific MCP server."""
    try:
        with _db() as conn:
            conn.execute("DELETE FROM mcp_cache WHERE mcp_server = ?", (server,))
    except Exception as e:
        warnings.warn(f"ToolRecall: MCP server invalidate failed: {e}")


def cached_mcp(
    server: str,
    tool: str,
    arguments: dict | None = None,
    fetch_fn: Callable | None = None,
    ttl: int | None = None,
) -> dict:
    """One-shot MCP cache: check → (optional) fetch → store → return.

    Usage:
        data = cached_mcp("fetch", "fetch", {"url": "..."},
                          fetch_fn=lambda: requests.get(url).json())
    """
    import json as _json

    result = cached_mcp_check(server, tool, arguments, ttl)
    if result.get("cached"):
        if result.get("stale") and fetch_fn is not None:
            # SWR: serve the stale data but refresh synchronously via the
            # caller's fetch_fn (bounded by the fetch itself — a background
            # thread cannot outlive this one-shot call). On fetch failure the
            # stale data stands (it is already in the caller's hands).
            try:
                data = fetch_fn()
                cached_mcp_store(
                    result["key"], server, tool, arguments or {}, _json.dumps(data), ttl
                )
            except Exception as e:
                warnings.warn(f"ToolRecall: SWR revalidate (mcp) failed: {e}")
            return _json.loads(result["data"])
        return _json.loads(result["data"])
    if fetch_fn is not None:
        data = fetch_fn()
        cached_mcp_store(result["key"], server, tool, arguments or {}, _json.dumps(data), ttl)
        return data
    return result


# ─── STATS & ADMIN ─────────────────────────────────────────


def _work_key_guard(key: Any) -> str:
    """Reject malformed work keys loudly — a ledger that accepts junk is a
    ledger that can serve the wrong unit's result."""
    if not isinstance(key, str) or not key.strip():
        raise ValueError("work key must be a non-empty string")
    return key


def work_store(key: Any, metrics: dict, eval_time: float = 0.0, ttl: float = 0.0) -> bool:
    """Record one PAID evaluation of a work unit (see ``keys.work_key``).

    Returns True when this call established the unit's result, False when the
    unit was already known. A stored result is never overwritten — a re-roll
    must not rewrite history — but ``attempts`` still advances, because the
    second evaluation was paid for and the reuse policy is exactly a question
    about how many times a unit was paid for.

    ``ttl=0`` (default) means no expiry: a work result is a statement about a
    deterministic unit, not a freshness-sensitive read. A caller memoizing an
    environment-dependent evaluator must pass a ttl or fold the environment
    into the key.
    """
    key = _work_key_guard(key)
    if not isinstance(metrics, dict):
        raise TypeError(f"metrics must be a dict, got {type(metrics).__name__}")
    payload = json.dumps(metrics, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    now = time.time()
    try:
        with _db() as conn:
            cur = conn.execute(
                """
                INSERT INTO work_ledger
                    (key, metrics_json, eval_time, attempts, created_at, updated_at, ttl)
                VALUES (?, ?, ?, 1, ?, ?, ?)
                ON CONFLICT(key) DO NOTHING
                """,
                (key, payload, float(eval_time), now, now, float(ttl)),
            )
            created = cur.rowcount == 1
            if not created:
                conn.execute(
                    "UPDATE work_ledger SET attempts = attempts + 1, updated_at = ? WHERE key = ?",
                    (now, key),
                )
        return created
    except Exception as e:  # a ledger write must never break the caller's run
        warnings.warn(f"ToolRecall: work ledger write failed for {key[:16]}: {e}")
        return False


def work_lookup(key: Any) -> dict | None:
    """Read a work unit's recorded result, or None when the unit is unknown.

    Counts into the ``work_cache`` category — hit when the unit is known, miss
    otherwise — so reuse is visible in ``top`` and the healthcheck. A malformed
    key raises; an unreadable ledger degrades to None, which makes the caller
    pay again instead of trusting a result we could not verify.
    """
    key = _work_key_guard(key)
    row = None
    try:
        with _db() as conn:
            row = conn.execute(
                """
                SELECT metrics_json, eval_time, attempts, created_at, updated_at, ttl
                FROM work_ledger WHERE key = ?
                """,
                (key,),
            ).fetchone()
            if row is not None:
                ttl = row["ttl"] or 0.0
                if ttl and (time.time() - (row["updated_at"] or 0.0)) > ttl:
                    conn.execute("DELETE FROM work_ledger WHERE key = ?", (key,))
                    row = None
    except Exception as e:  # unreadable ledger: pay again rather than trust it
        warnings.warn(f"ToolRecall: work ledger read failed for {key[:16]}: {e}")
        return None
    _record("work_cache", hit=row is not None, path=key[:64])
    if row is None:
        return None
    try:
        metrics = json.loads(row["metrics_json"])
    except (TypeError, ValueError):
        metrics = {}
    return {
        "metrics": metrics,
        "eval_time": row["eval_time"] or 0.0,
        "attempts": row["attempts"],
        "age_s": max(0.0, time.time() - (row["created_at"] or 0.0)),
    }


def get_stats() -> dict:
    """Get cache statistics with honest token accounting.

    ``tokens_served_from_cache`` (née ``io_tokens_saved``) is the real
    cumulative accumulator — each cache hit records the estimated token
    count of the content served, so this is the actual number of tokens
    served from cache instead of being re-read from disk.

    ``cached_content_tokens`` shows the estimated token size of ALL unique
    content in the file_cache (byte sum / 4 chars-per-token heuristic) —
    a measure of cache capacity, NOT savings.

    ``tokens_served_from_cache_cumulative`` preserves the raw DB
    accumulator for debugging. ``tokens_served_from_cache_adjusted``
    (file_cache only) re-counts based on SQLite re-reads only, excluding
    the first unavoidable disk read per file — mirrors
    bench/cache_honest.py logic.
    ``tokens_not_sent_to_llm`` tracks only agent-tool-initiated reads
    (tagged with source="agent_tool") — tokens that never entered the LLM
    context. It IS the same number as ``tokens_saved_to_llm``.

    Canonical names (2026-09-26 rename) — the DB column and the JSON key are
    now the same word, and the two savings concepts are cleanly split:
      ``io_tokens_saved``     — IO offload: tokens served from the cache
        instead of being re-read from disk / re-fetched (latency metric).
      ``tokens_saved_to_llm`` — tokens that never entered the LLM context
        (the cost/savings metric).
    Older spellings remain as back-compat aliases: ``tokens_served_from_cache``
    / ``tokens_not_read_from_disk`` (= io_tokens_saved) and
    ``tokens_not_sent_to_llm`` (= tokens_saved_to_llm). The ambiguous
    ``context_tokens_saved`` / ``tokens_saved`` names are gone (DB migration
    renames the columns in place).
    """
    stats: dict[str, Any] = {}
    try:
        with _db() as conn:
            # Lifetime view: the GC banks the four counters it resets for idle
            # categories (cache_stats_banked), so a total a user cares about
            # cannot be destroyed by housekeeping. Table check instead of
            # try/except: a missing table on a pre-migration DB is expected,
            # and a swallowed exception here would hide real read errors.
            banked: dict[str, tuple] = {}
            if conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='cache_stats_banked'"
            ).fetchone():
                bank_cols = {r[1] for r in conn.execute("PRAGMA table_info(cache_stats_banked)")}
                # "0" keeps this readable on a bank table from an older build
                # that has no ctx column yet (the migration adds it, but a
                # read-only or not-yet-migrated DB must not break the view).
                _ctx_col = "tokens_saved_to_llm" if "tokens_saved_to_llm" in bank_cols else "0"
                # Same guard for the two axes reset_stats() also moves. Kept as
                # trailing SELECT columns on purpose: the first seven tuple
                # slots (hits, misses, read, io, ctx, banked_at, bank_count)
                # must keep their meaning for the lifetime_* fields below.
                _unc_col = (
                    "tokens_read_uncacheable" if "tokens_read_uncacheable" in bank_cols else "0"
                )
                _meas_col = (
                    "tokens_not_sent_measured" if "tokens_not_sent_measured" in bank_cols else "0"
                )
                banked = {
                    r[0]: (r[1], r[2], r[3], r[4], r[5], r[6], r[7], r[8], r[9])
                    for r in conn.execute(
                        "SELECT category, hits, misses, tokens_read_from_disk,"
                        " io_tokens_saved, " + _ctx_col + ","
                        " banked_at, bank_count,"
                        " " + _unc_col + ", " + _meas_col + " FROM cache_stats_banked"
                    )
                }
            for row in conn.execute("SELECT * FROM cache_stats"):
                total = row["hits"] + row["misses"]
                b = banked.get(row["category"], (0, 0, 0, 0, 0, None, 0, 0, 0))
                measured = (
                    row["tokens_not_sent_measured"]
                    if "tokens_not_sent_measured" in row.keys()
                    else 0
                )
                lifetime_hits = row["hits"] + b[0]
                lifetime_misses = row["misses"] + b[1]
                lifetime_total = lifetime_hits + lifetime_misses
                stats[row["category"]] = {
                    "hits": row["hits"],
                    "misses": row["misses"],
                    "tokens_served_from_disk": row["tokens_read_from_disk"],
                    # Names describe WHAT HAPPENED TO THE TOKENS, not what
                    # the cache did internally:
                    #   tokens_served_from_cache — tokens served from the
                    #     cache instead of from disk (latency/IO metric).
                    #   tokens_served_from_disk  — tokens actually read
                    #     from disk on cacheable misses (miss denominator).
                    #   tokens_read_uncacheable  — ttl=0 reads; NO cache
                    #     could serve them, kept OUT of the miss denominator
                    #     (the 2026-09 "2% savings" artifact).
                    #   tokens_not_sent_to_llm   — tokens that never entered
                    #     the LLM context (actual cost/savings metric; only
                    #     agent-tool reads can drop content from context).
                    "tokens_read_uncacheable": row["tokens_read_uncacheable"]
                    if "tokens_read_uncacheable" in row.keys()
                    else 0,
                    "tokens_served_from_cache": row["io_tokens_saved"],
                    "tokens_served_from_cache_cumulative": row["io_tokens_saved"],
                    # Canonical names (2026-09-26): same word in DB and JSON.
                    "io_tokens_saved": row["io_tokens_saved"],
                    "tokens_saved_to_llm": row["tokens_saved_to_llm"],
                    "tokens_not_sent_to_llm": row["tokens_saved_to_llm"],
                    # Lifetime = live + banked. The live columns are what
                    # happened since the last GC reset; these are what happened
                    # ever. Both are honest; a reader who wants "total" reads
                    # these, and never has to wonder whether housekeeping
                    # silently dropped a number (it did, until 2026-10-03).
                    "lifetime_hits": row["hits"] + b[0],
                    "lifetime_misses": row["misses"] + b[1],
                    "lifetime_tokens_read_from_disk": row["tokens_read_from_disk"] + b[2],
                    "lifetime_io_tokens_saved": row["io_tokens_saved"] + b[3],
                    "banked_io_tokens_saved": b[3],
                    # Same banking guarantee for the ctx axis. It used to be
                    # reset without being banked, so the lifetime view could
                    # report MORE context savings than IO offload — impossible,
                    # since ctx savings are a subset of IO offload.
                    "lifetime_tokens_saved_to_llm": row["tokens_saved_to_llm"] + b[4],
                    "banked_tokens_saved_to_llm": b[4],
                    "banked_at": b[5],
                    "bank_count": b[6],
                    # The two axes reset_stats() moves but the GC leaves alone:
                    # without a lifetime field a reset would drop them from the
                    # report entirely (the measured axis is the only wire-level
                    # verified savings number there is).
                    "lifetime_tokens_read_uncacheable": (
                        (
                            row["tokens_read_uncacheable"]
                            if "tokens_read_uncacheable" in row.keys()
                            else 0
                        )
                        + b[7]
                    ),
                    "lifetime_tokens_not_sent_measured": measured + b[8],
                    "banked_tokens_not_sent_measured": b[8],
                    # MEASURED tier — wire-verified (bridge replaced the
                    # content with a stub). Never an estimate; independent
                    # of any checkpoint/hint state.
                    "tokens_not_sent_measured": measured,
                    # Back-compat aliases (v0.10.x → v0.11+).
                    "tokens_read_from_disk": row["tokens_read_from_disk"],
                    "tokens_not_read_from_disk": row["io_tokens_saved"],
                    "tokens_not_read_from_disk_cumulative": row["io_tokens_saved"],
                    # tokens_saved_to_llm is emitted with the canonical block above.
                    "updated_at": row["updated_at"],
                    # An empty window observed NOTHING — "0%" would claim a
                    # measured all-miss rate that never happened (a fresh
                    # reset reads 0% next to a lifetime 96%, which is exactly
                    # the confusion this avoids). "-" matches the TUI, which
                    # has rendered an empty window as "-" all along.
                    "hit_rate": f"{row['hits'] / total * 100:.0f}%" if total > 0 else "-",
                    "lifetime_hit_rate": (
                        f"{lifetime_hits / lifetime_total * 100:.0f}%"
                        if lifetime_total > 0
                        else "-"
                    ),
                }
                if row["category"] == "file_cache":
                    total_bytes = conn.execute(
                        "SELECT COALESCE(SUM(size), 0) FROM file_cache"
                    ).fetchone()[0]
                    stats[row["category"]]["cached_content_tokens"] = total_bytes // 4
                    stats[row["category"]]["unique_files"] = conn.execute(
                        "SELECT COUNT(*) FROM file_cache"
                    ).fetchone()[0]
                    # Adjusted tokens_served_from_cache: net savings after subtracting unavoidable
                    # first-read disk I/O. This is the honest signal — how many tokens
                    # the cache actually served beyond the one mandatory read per file.
                    # Mirrors bench/cache_honest.py philosophy.
                    raw_saved = stats[row["category"]].get("tokens_served_from_cache", 0)
                    disk_read = stats[row["category"]].get("tokens_served_from_disk", 0)
                    stats[row["category"]]["tokens_served_from_cache_adjusted"] = max(
                        0, raw_saved - disk_read
                    )
                    # Back-compat alias
                    stats[row["category"]]["tokens_not_read_from_disk_adjusted"] = stats[
                        row["category"]
                    ]["tokens_served_from_cache_adjusted"]
            for t in [
                "file_cache",
                "skill_cache",
                "terminal_cache",
                "script_cache",
                "code_cache",
                "mcp_cache",
                "browser_cache",
            ]:
                r = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()
                stats[f"{t}_entries"] = r[0]
    except Exception as e:
        warnings.warn(f"ToolRecall: SQLite stats failed: {e}")

    # Zero tokens_saved_to_llm when the agent can't drop content.
    # The DB still holds the cumulative value, but reporting it as
    # "saved" is misleading when emit_context_hints is off.
    # Load config once — re-parsing TOML every call is wasteful.
    try:
        from toolrecall.config import load_config as _load_config

        _cfg = _load_config()
        _hints_on = _cfg.mcp_emit_context_hints
    except Exception:
        _hints_on = True  # safe default — don't zero if config broken
    if not _hints_on:
        for _cat in stats:
            if isinstance(stats[_cat], dict):
                for _key in ("tokens_not_sent_to_llm", "tokens_saved_to_llm"):
                    if _key in stats[_cat]:
                        stats[_cat][_key] = 0

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

    # Storage backend info — delegated so cache.py stays backend-agnostic
    try:
        from toolrecall.storage import stats_info

        stats.update(stats_info(config))
    except Exception:
        stats["storage_backend"] = "unknown"
        stats["sync_enabled"] = False
        stats["sync_url"] = None
        stats["sync_interval"] = None

    return stats


#: Every cache_stats column that accumulates over the window. reset_stats()
#: moves all of them into cache_stats_banked before zeroing the live row.
#: This is deliberately a superset of what garbage_collect() banks: the GC
#: resets only its idle-category columns and must leave the measured
#: bridge-stub axis and the uncacheable-read denominator alone (zeroing those
#: would blank the only wire-level verified savings headline), while a
#: user-requested reset zeroes the whole row.
_BANKED_COUNTERS = (
    "hits",
    "misses",
    "tokens_read_from_disk",
    "io_tokens_saved",
    "tokens_saved_to_llm",
    "tokens_read_uncacheable",
    "tokens_not_sent_measured",
)


def _ensure_cache_stats_banked(conn) -> set:
    """Create the bank table; add any counter column an older build lacks.

    Self-sufficient on a pre-migration DB: a missing table or a table created
    before a column existed must never abort the caller (same guard style as
    the GC's inline version). Returns the column set present afterwards.
    """
    conn.execute(
        "CREATE TABLE IF NOT EXISTS cache_stats_banked ("
        " category TEXT PRIMARY KEY,"
        " hits INTEGER NOT NULL DEFAULT 0,"
        " misses INTEGER NOT NULL DEFAULT 0,"
        " tokens_read_from_disk INTEGER NOT NULL DEFAULT 0,"
        " io_tokens_saved INTEGER NOT NULL DEFAULT 0,"
        " tokens_saved_to_llm INTEGER NOT NULL DEFAULT 0,"
        " tokens_read_uncacheable INTEGER NOT NULL DEFAULT 0,"
        " tokens_not_sent_measured INTEGER NOT NULL DEFAULT 0,"
        " banked_at REAL,"
        " bank_count INTEGER NOT NULL DEFAULT 0)"
    )
    have = {r[1] for r in conn.execute("PRAGMA table_info(cache_stats_banked)")}
    for col in _BANKED_COUNTERS:
        if col not in have:
            conn.execute(
                f"ALTER TABLE cache_stats_banked ADD COLUMN {col} INTEGER NOT NULL DEFAULT 0"
            )
            have.add(col)
    return have


def _bank_and_zero_cache_stats(conn, now: float) -> list:
    """Move every cache_stats counter into the bank, then zero the live row.

    The move SUMS rather than overwrites: a category already banked by an
    earlier reset or by the GC must accumulate, not lose the earlier total
    (bank_count records how often it moved). Returns the moved categories.
    """
    _ensure_cache_stats_banked(conn)
    cols = ", ".join(_BANKED_COUNTERS)
    placeholders = ", ".join(["?"] * len(_BANKED_COUNTERS))
    rows = conn.execute(f"SELECT category, {cols} FROM cache_stats").fetchall()
    for row in rows:
        conn.execute(
            f"INSERT INTO cache_stats_banked (category, {cols}, banked_at, bank_count)"
            f" VALUES (?, {placeholders}, ?, 1)"
            " ON CONFLICT(category) DO UPDATE SET "
            + ", ".join(f"{c} = {c} + excluded.{c}" for c in _BANKED_COUNTERS)
            + ", banked_at = excluded.banked_at, bank_count = bank_count + 1",
            (*row, now),
        )
    if rows:
        conn.execute(
            "UPDATE cache_stats SET "
            + ", ".join(f"{c} = 0" for c in _BANKED_COUNTERS)
            + ", updated_at = ?",
            (now,),
        )
    return [r[0] for r in rows]


def _bank_access_log(conn) -> int:
    """Compact the access_log window into access_log_banked; drop the raw rows.

    A reset used to DELETE this table, which silently destroyed the trend
    window: `toolrecall trend` aggregates access_log at query time and has no
    other source (see trend.py). Per day/category/path counts keep every read
    and hit counted at a fraction of the rows — a MOVE, not a discard.
    Returns the number of (day, category) buckets now on record.
    """
    conn.execute(
        "CREATE TABLE IF NOT EXISTS access_log_banked ("
        " day TEXT NOT NULL,"
        " category TEXT NOT NULL,"
        " path TEXT NOT NULL DEFAULT '',"
        " reads INTEGER NOT NULL DEFAULT 0,"
        " hits INTEGER NOT NULL DEFAULT 0,"
        " misses INTEGER NOT NULL DEFAULT 0,"
        " tokens INTEGER NOT NULL DEFAULT 0,"
        " banked_at REAL,"
        " bank_count INTEGER NOT NULL DEFAULT 0,"
        " PRIMARY KEY (day, category, path))"
    )
    # INSERT ... SELECT with ON CONFLICT needs a WHERE clause: without one
    # SQLite parses the ON as a join condition and reports a syntax error.
    conn.execute(
        """
        INSERT INTO access_log_banked
            (day, category, path, reads, hits, misses, tokens, banked_at, bank_count)
        SELECT date(cached_at, 'unixepoch'),
               category,
               coalesce(path, ''),
               COUNT(*),
               SUM(hit),
               SUM(1 - hit),
               SUM(coalesce(tokens, 0)),
               ?,
               1
        FROM access_log
        WHERE category IS NOT NULL
        GROUP BY 1, 2, 3
        ON CONFLICT(day, category, path) DO UPDATE SET
            reads = reads + excluded.reads,
            hits = hits + excluded.hits,
            misses = misses + excluded.misses,
            tokens = tokens + excluded.tokens,
            banked_at = excluded.banked_at,
            bank_count = bank_count + 1
        """,
        (time.time(),),
    )
    buckets = conn.execute(
        "SELECT COUNT(*) FROM (SELECT 1 FROM access_log_banked GROUP BY day, category)"
    ).fetchone()[0]
    conn.execute("DELETE FROM access_log")
    return int(buckets or 0)


def reset_stats():
    """Reset the statistics *window* without destroying any history.

    MOVE, never destroy. Two silent wipes (2026-09-08/09) destroyed lifetime
    counters and the access_log trend window before anyone noticed, and the
    trend command has no other source — a reset blanked it silently. So:

      1. every cache_stats counter is moved into cache_stats_banked and the
         live row is zeroed: the window reads 0, the lifetime total
         (get_stats lifetime_*) still carries everything;
      2. the access_log window is compacted into access_log_banked before the
         raw rows go, so `toolrecall trend` still reaches back past a reset.

    Cache *entries* were never touched by this function and still aren't.

    AUDIT: every move is logged with the calling module — the 2026-09-10 wipe
    storm (17 resets in 5h) was attributable only by timestamp correlation.
    This must never happen silently again.
    """
    import logging
    import traceback

    try:
        with _db() as conn:
            window_rows = conn.execute("SELECT COUNT(*) FROM access_log").fetchone()[0]
            moved = _bank_and_zero_cache_stats(conn, time.time())
            buckets = _bank_access_log(conn)
        # Caller forensics: the 2026-09-10 wipe storm (17 resets in 5h) was
        # attributable only by timestamp correlation — the audit line named no
        # caller. Frame list is path/line/function only (payload-free, no
        # source text): enough to name the offending test/script immediately.
        caller_frames = " <- ".join(
            f"{fr.filename}:{fr.lineno}:{fr.name}" for fr in traceback.extract_stack()[-7:-1]
        )
        logging.getLogger(__name__).warning(
            "reset_stats: moved %d cache_stats row(s) into cache_stats_banked"
            " and compacted %d access_log row(s) into %d banked (day, category)"
            " bucket(s) — no lifetime total destroyed | caller should be an"
            " explicit user action | caller stack: %s",
            len(moved),
            window_rows,
            buckets,
            caller_frames,
        )
    except Exception as e:
        warnings.warn(f"ToolRecall: reset_stats failed: {e}")


def invalidate_all():
    """Clear all agent caches (memory + SQLite).

    Scope: file/skill/terminal/script/code/mcp/browser caches — the caches
    whose entries can be regenerated by re-reading or re-running a tool.

    api_cache is deliberately NOT cleared: its rows are billed LLM
    responses, so wiping them re-bills every replayed call, and in-flight
    proxy stores re-insert immediately, making a full wipe best-effort
    anyway. Reachable paths: cache_invalidate(scope="api") over IPC/the
    MCP tool (full table) or programmatic invalidate_api_cache();
    per-host targeting is programmatic-only via invalidate_api_host(host)
    (the IPC scope enum has no host argument).
    """
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


def invalidate_file(path: str, ns: str = ""):
    """Invalidate a specific file from cache (memory + SQLite).

    A6-7: with a namespace, ONLY that scope's entry is removed — global
    and other scopes' entries for the same path survive.

    Also clears the shim's unchanged-file stub state for the path
    (Lever 1): after a write/patch, the next read must deliver FULL
    content (the agent's held copy is stale).
    """
    path = os.path.expanduser(path)
    if ns:
        _file_cache.remove(f"{ns}\x00{path}")
        h = _hash(f"{ns}\x00{path}")
    else:
        _file_cache.remove(path)
        h = _hash(path)

    try:
        from toolrecall.shim_stub import reset_path as _stub_reset

        _stub_reset(path)
    except Exception:
        # stub-state reset must never block invalidation (daemon-side call:
        # the shim's in-process state is unreachable from here — mtime-gate
        # covers correctness; the swallow is logged for diagnosis).
        log.debug("stub-state reset failed for %s", path, exc_info=True)

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
            # recall_cache may predate the expires_at column (added by the
            # v0.8.20 migration in _db._init). Check PRAGMA instead of
            # catching OperationalError: a missing column previously aborted
            # the WHOLE GC (terminal/mcp purge + VACUUM never ran).
            recall_cols = {row[1] for row in conn.execute("PRAGMA table_info(recall_cache)")}
            c4 = 0
            if "expires_at" in recall_cols:
                c4 = conn.execute(
                    "DELETE FROM recall_cache WHERE expires_at IS NOT NULL AND expires_at < ?",
                    (now,),
                ).rowcount
            ONE_DAY = 86400
            # api_cache rows DO expire (API_CACHE_TTL, default 300 s) but nothing
            # ever reclaimed them — measured on the live DB: 12,281 of 12,385 rows
            # were expired and held 1,103 MB of a 1,190 MB file. Reclaiming them
            # (this purge + the VACUUM below) took the DB to 86 MB (13.9x smaller).
            # NB the first run after this change pays that backlog once — measured
            # 65.8 s purge + 14.2 s VACUUM, all under the DB lock — so on a busy
            # instance do the initial reclaim during a maintenance window rather
            # than letting a live GC thread hold the lock for a minute. Steady
            # state is trivial (a few hundred rows per 4 h cadence).
            # Same guard style as recall_cache: a pre-migration DB may lack the
            # column, and a missing column must never abort the whole GC (which
            # would also skip the VACUUM that actually returns the space).
            api_cols = {row[1] for row in conn.execute("PRAGMA table_info(api_cache)")}
            c5 = 0
            if "expires_at" in api_cols:
                c5 = conn.execute(
                    "DELETE FROM api_cache WHERE expires_at IS NOT NULL AND expires_at < ?",
                    (now,),
                ).rowcount
            # browser_cache had NO reclaimer at all: nothing purged it and the
            # check did not expire rows either, so a cached page lived as long
            # as the DB and the table only grew. Purge on the same TTL the
            # check enforces (_browser_row_expired), so an expired page is
            # neither served nor kept. Same guard style as above: a
            # pre-migration DB must never abort the whole GC.
            browser_cols = {row[1] for row in conn.execute("PRAGMA table_info(browser_cache)")}
            c6 = 0
            if "cached_at" in browser_cols:
                c6 = conn.execute(
                    "DELETE FROM browser_cache WHERE cached_at IS NULL OR cached_at < ?",
                    (now - BROWSER_CACHE_TTL,),
                ).rowcount
            # Never destroy a lifetime total. The previous form zeroed
            # hits/misses/io_tokens_saved for every category idle >24 h and
            # wrote updated_at = now in the same statement, so the wipe looked
            # like activity and nothing was logged: the live DB went from
            # 551,803 hits / 2.02 B io to 1,675 / 1.9 M while the two ctx
            # columns — which the statement did not touch — kept growing, i.e.
            # the display ended up claiming more context savings than IO
            # offload (impossible: ctx ⊆ io). Statement dates from 36d27d0
            # (2026-07-04); found and reproduced 2026-10-03.
            # Bank the five reset columns, then reset the live row.
            idle = conn.execute(
                "SELECT category, hits, misses, tokens_read_from_disk, io_tokens_saved,"
                " tokens_saved_to_llm"
                " FROM cache_stats WHERE updated_at > 0 AND updated_at < ?",
                (now - ONE_DAY,),
            ).fetchall()
            c3 = 0
            if idle:
                # Self-sufficient on a pre-migration DB: a missing bank table
                # must never abort the whole GC (the same guard style as the
                # column checks above). Caught by
                # test_daemon_error_honesty::test_gc_with_legacy_recall_cache.
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS cache_stats_banked ("
                    " category TEXT PRIMARY KEY,"
                    " hits INTEGER NOT NULL DEFAULT 0,"
                    " misses INTEGER NOT NULL DEFAULT 0,"
                    " tokens_read_from_disk INTEGER NOT NULL DEFAULT 0,"
                    " io_tokens_saved INTEGER NOT NULL DEFAULT 0,"
                    " banked_at REAL,"
                    " bank_count INTEGER NOT NULL DEFAULT 0)"
                )
                # CREATE TABLE IF NOT EXISTS is a no-op on a bank table created
                # by an older build, so add the column explicitly. Missing it
                # would make every banked ctx total silently vanish from the
                # lifetime view — the same "impossible totals" class of bug the
                # comment above documents.
                _bank_cols = {r[1] for r in conn.execute("PRAGMA table_info(cache_stats_banked)")}
                if "tokens_saved_to_llm" not in _bank_cols:
                    conn.execute(
                        "ALTER TABLE cache_stats_banked ADD COLUMN"
                        " tokens_saved_to_llm INTEGER NOT NULL DEFAULT 0"
                    )
                for row in idle:
                    conn.execute(
                        """
                        INSERT INTO cache_stats_banked
                            (category, hits, misses, tokens_read_from_disk,
                             io_tokens_saved, tokens_saved_to_llm, banked_at, bank_count)
                        VALUES (?, ?, ?, ?, ?, ?, ?, 1)
                        ON CONFLICT(category) DO UPDATE SET
                            hits = hits + excluded.hits,
                            misses = misses + excluded.misses,
                            tokens_read_from_disk =
                                tokens_read_from_disk + excluded.tokens_read_from_disk,
                            io_tokens_saved = io_tokens_saved + excluded.io_tokens_saved,
                            tokens_saved_to_llm =
                                tokens_saved_to_llm + excluded.tokens_saved_to_llm,
                            banked_at = excluded.banked_at,
                            bank_count = bank_count + 1
                    """,
                        (*row, now),
                    )
                c3 = conn.execute(
                    """
                    UPDATE cache_stats SET
                        hits = 0,
                        misses = 0,
                        tokens_read_from_disk = 0,
                        io_tokens_saved = 0,
                        tokens_saved_to_llm = 0,
                        updated_at = ?
                    WHERE updated_at > 0 AND updated_at < ?
                """,
                    (now, now - ONE_DAY),
                ).rowcount
                log.warning(
                    "GC: banked %d idle cache_stats row(s) before reset (%s) —"
                    " lifetime totals stay readable as live + banked",
                    c3,
                    ", ".join(str(r[0]) for r in idle),
                )
        # VACUUM must run in its own connection (auto-commits, can't be in a transaction).
        # Gated — see _VACUUM_MIN_FREE_*: a rewrite is only worth the write-lock stall
        # when it returns a meaningful share of the file. The decision is logged either
        # way so a skipped compaction is visible instead of silent.
        with _db() as conn:
            page_size = conn.execute("PRAGMA page_size").fetchone()[0]
            size_bytes = max(conn.execute("PRAGMA page_count").fetchone()[0] * page_size, 1)
            free_bytes = conn.execute("PRAGMA freelist_count").fetchone()[0] * page_size
            floor = int(
                float(config.get("cache", "vacuum_min_free_mb", default=_VACUUM_MIN_FREE_MB))
                * 1048576
            )
            threshold = max(floor, int(size_bytes * _VACUUM_MIN_FREE_RATIO))
            if free_bytes >= threshold:
                conn.execute("VACUUM")
                log.info(
                    "GC: VACUUM ran (free %.1f MB of %.1f MB, threshold %.1f MB)",
                    free_bytes / 1048576,
                    size_bytes / 1048576,
                    threshold / 1048576,
                )
            else:
                log.info(
                    "GC: VACUUM skipped (free %.1f MB of %.1f MB, threshold %.1f MB)",
                    free_bytes / 1048576,
                    size_bytes / 1048576,
                    threshold / 1048576,
                )
        return c1 + c2 + c3 + c4 + c5 + c6
    except Exception as e:
        warnings.warn(f"ToolRecall GC failed: {e}")
        return -1


# ─── BROWSER PAGE CACHE (SQLite, key-value) ──────────────

BROWSER_CACHE_TTL = 3600  # 1 hour default for browser page cache


def _browser_row_expired(cached_at: float | None, now: float) -> bool:
    """True when a browser_cache row is past its TTL.

    Used by BOTH the check and the GC so an expired page is neither served
    nor kept. Export was the bug: the constant existed but nothing read it,
    so a cached page was served for the lifetime of the DB while the table
    only ever grew. A NULL cached_at is treated as expired — the
    conservative direction is a refetch, never a stale serve.
    """
    if cached_at is None:
        return True
    return (now - cached_at) > BROWSER_CACHE_TTL


def cached_browser_check(
    cache_key: str,
) -> dict:
    """Check if browser page content is cached by key.

    Expired rows are a miss (see ``_browser_row_expired``): a browser page is
    point-in-time content, so serving one past the TTL is a stale delivery,
    not a saving.

    Args:
        cache_key: e.g. ``browser:page:https_example_com:snapshot``

    Returns:
        ``{"cached": True, "content": "...", "tokens_not_read_from_disk": N}`` on hit,
        ``{"cached": False}`` on miss.
    """
    try:
        with _db() as conn:
            now = time.time()
            row = conn.execute(
                "SELECT content, hits, cached_at FROM browser_cache WHERE cache_key = ?",
                (cache_key,),
            ).fetchone()
            if row is not None and not _browser_row_expired(row["cached_at"], now):
                conn.execute(
                    "UPDATE browser_cache SET hits = hits + 1 WHERE cache_key = ?",
                    (cache_key,),
                )
                # io_tokens_saved was computed but never passed (2026-09-28):
                # the TUI IO-offload column read 0 for browser hits.
                io_tokens_saved = _estimate_tokens(row["content"])
                _record("browser_cache", True, io_tokens_saved=io_tokens_saved)
                return {
                    "cached": True,
                    "content": row["content"],
                    "tokens_not_read_from_disk": io_tokens_saved,
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

# Bodies larger than this are NOT stored (see config.api_cache_max_body_mb):
# a truncated body could be replayed as a hit and hand the caller a corrupt
# response, so oversize bodies are skipped whole rather than shortened.
API_CACHE_MAX_BODY_MB = 2.0


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
                "SELECT response_status, response_headers, response_body, expires_at, hit_streak "
                "FROM api_cache WHERE request_hash = ?",
                (request_hash,),
            ).fetchone()
            if row is not None:
                from toolrecall import ttl_policy

                tier = ttl_policy.classify_expiry(row["expires_at"], now, _swr_window())
                import json as _json

                headers = _json.loads(row["response_headers"]) if row["response_headers"] else {}
                io_tokens_saved = _estimate_tokens(row["response_body"])
                if tier == ttl_policy.SWR_FRESH:
                    conn.execute(
                        "UPDATE api_cache SET hits = hits + 1, hit_streak = hit_streak + 1 "
                        "WHERE request_hash = ?",
                        (request_hash,),
                    )
                    # io_tokens_saved was computed but never passed (2026-09-21):
                    # api_cache showed 122 hits with io_tokens_saved=0, hiding a
                    # whole-call saving — the strongest one there is, since the
                    # model is not called at all.
                    _record("api_cache", True, io_tokens_saved=io_tokens_saved)
                    return {
                        "cached": True,
                        "status": row["response_status"],
                        "headers": headers,
                        "body": row["response_body"],
                        "tokens_not_read_from_disk": io_tokens_saved,
                    }
                if tier == ttl_policy.SWR_SERVE_STALE:
                    # SWR serve-stale ONLY. An LLM completion is never
                    # auto-replayed in the background (cost + non-idempotent
                    # billing); the next non-stale pass refreshes the row.
                    conn.execute(
                        "UPDATE api_cache SET hits = hits + 1 WHERE request_hash = ?",
                        (request_hash,),
                    )
                    _record("api_cache", True, io_tokens_saved=io_tokens_saved)
                    return {
                        "cached": True,
                        "status": row["response_status"],
                        "headers": headers,
                        "body": row["response_body"],
                        "tokens_not_read_from_disk": io_tokens_saved,
                        "stale": True,
                    }
                # tier == "expired" → fall through to miss
    except Exception as e:
        msg = str(e)
        if "malformed" in msg:
            warnings.warn(f"ToolRecall: api_cache check failed — DB corruption detected: {e}")
            _recover_database(str(config.get("storage", "db_path", default="")))
        else:
            warnings.warn(f"ToolRecall: api_cache check failed: {e}")

    _record("api_cache", False)
    return {"cached": False}


def cached_api_store(
    request_hash: str,
    method: str,
    host: str,
    path: str,
    request_body_hash: str,
    response_status: int,
    response_headers: dict,
    response_body: str,
    ttl: int | None = None,
) -> dict:
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

    # Skip oversize bodies outright (never truncate — a truncated body would be
    # served as a hit and would be a corrupt response to the caller).
    # Callers pass both str and bytes bodies, so size the raw payload as-is.
    try:
        max_bytes = int(
            float(config.get("cache", "api_cache_max_body_mb", default=API_CACHE_MAX_BODY_MB))
            * 1048576
        )
    except (TypeError, ValueError):
        max_bytes = int(API_CACHE_MAX_BODY_MB * 1048576)
    n_bytes = (
        len(response_body)
        if isinstance(response_body, (bytes, bytearray))
        else len(response_body.encode("utf-8", "replace"))
    )
    if n_bytes > max_bytes:
        return {
            "stored": False,
            "reason": "body_too_large",
            "bytes": n_bytes,
            "max_bytes": max_bytes,
        }

    try:
        with _db() as conn:
            # Adaptive TTL: stretch by the existing row's hit streak when the
            # fresh response is unchanged from the cached one.
            old = conn.execute(
                "SELECT response_body, hit_streak FROM api_cache WHERE request_hash = ?",
                (request_hash,),
            ).fetchone()
            eff_ttl = _next_ttl(ttl, old if old and old["response_body"] == response_body else None)
            conn.execute(
                """INSERT OR REPLACE INTO api_cache
                   (request_hash, method, host, path, request_body_hash,
                    request_body_preview, response_status, response_headers,
                    response_body, cached_at, expires_at, hit_streak)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    request_hash,
                    method,
                    host,
                    path,
                    request_body_hash,
                    response_body[:200],
                    response_status,
                    _json.dumps(response_headers),
                    response_body,
                    now,
                    now + eff_ttl,
                    0,
                ),
            )
        _record_tokens_read_from_disk("api_cache", _estimate_tokens(response_body))
        return {"stored": True}
    except Exception as e:
        warnings.warn(f"ToolRecall: api_cache store failed: {e}")
        return {"stored": False, "error": str(e)}


def invalidate_api_cache() -> int:
    """Explicitly clear the whole api_cache table (billed LLM responses).

    NOT part of invalidate_all() by design — see its docstring. Returns
    the number of deleted rows.
    """
    try:
        with _db() as conn:
            return conn.execute("DELETE FROM api_cache").rowcount
    except Exception as e:
        warnings.warn(f"ToolRecall: api_cache invalidate failed: {e}")
        return 0


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
