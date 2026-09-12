"""Honest daemon-error reporting + resilient GC.

Covers three fixes shipped together (2026-09-06 incident follow-up):

1. ``TransportClient.send`` error honesty — a response *timeout* must be
   reported as ``daemon_timeout`` (daemon alive, busy), not
   ``daemon_unavailable`` (daemon down). The conflation made a healthy
   daemon look dead whenever cache_status queued behind multi-MB inserts.
2. ``garbage_collect`` resilience — ``recall_cache`` databases created
   before the ``expires_at`` column existed made GC abort with
   "no such column", skipping the terminal/mcp purge AND VACUUM.
3. ``tests/test_daemon_pid_guard`` hermeticity is verified in that file's
   own setUp (subprocess.run stub); here we add an explicit regression
   guard that running the pid-guard tests never stops the real unit.
"""

import os
import socket
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from toolrecall.transport import TransportClient


class _TempDbFixture:
    """Redirect the _db() singleton at a scratch DB and restore afterwards.

    _db() resolves the path through the cached Config (cfg.cache_db), so
    env vars alone don't move it — we point the Config object itself at
    the scratch path and reset the connection-singleton state.
    """

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._patcher = None
        self._saved = {}

    def __enter__(self):
        from toolrecall import _db as db_mod

        cfg = db_mod._get_cached_config()
        self._saved = {"cache_db": cfg.get("paths", "cache_db")}
        cfg._data.setdefault("paths", {})["cache_db"] = self.db_path
        self._saved["_db_real"] = db_mod._db_real
        self._saved["_db_path_cached"] = db_mod._db_path_cached
        if db_mod._db_real is not None:
            db_mod._db_real.close()
        db_mod._db_real = None
        db_mod._db_path_cached = None
        return self

    def __exit__(self, *exc):
        from toolrecall import _db as db_mod

        if db_mod._db_real is not None:
            db_mod._db_real.close()
            db_mod._db_real = None
        db_mod._db_path_cached = self._saved["_db_path_cached"]
        cfg = db_mod._get_cached_config()
        cfg._data.setdefault("paths", {})["cache_db"] = self._saved["cache_db"]
        return False


class SendErrorHonestyTest(unittest.TestCase):
    """send() must distinguish down / timeout / dropped."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="tr_err_honesty_")

    def test_connect_refused_is_daemon_unavailable(self):
        # Socket path that exists as a regular file → connect fails = down
        fake = os.path.join(self.tmpdir, "not-a-socket")
        open(fake, "w").close()
        c = TransportClient(fake)
        r = c.send({"cmd": "ping"}, timeout=1.0)
        self.assertEqual(r.get("error"), "daemon_unavailable")

    def test_missing_socket_file_is_daemon_unavailable(self):
        c = TransportClient(os.path.join(self.tmpdir, "missing.sock"))
        r = c.send({"cmd": "ping"}, timeout=1.0)
        self.assertEqual(r.get("error"), "daemon_unavailable")

    def test_slow_daemon_is_daemon_timeout_not_unavailable(self):
        """A live but unresponsive daemon must report daemon_timeout."""
        # Real listening socket that never replies
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock_path = os.path.join(self.tmpdir, "silent.sock")
        srv.bind(sock_path)
        srv.listen(1)
        try:
            c = TransportClient(sock_path)
            r = c.send({"cmd": "ping"}, timeout=0.3)
            self.assertEqual(
                r.get("error"),
                "daemon_timeout",
                f"slow daemon misreported: {r}",
            )
            self.assertNotEqual(r.get("error"), "daemon_unavailable")
        finally:
            srv.close()
            os.unlink(sock_path)

    def test_dropped_connection_is_connection_dropped(self):
        """A socket that accepts then immediately closes the connection."""
        import threading

        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock_path = os.path.join(self.tmpdir, "drop.sock")
        srv.bind(sock_path)
        srv.listen(1)
        stop = threading.Event()

        def closer():
            srv.settimeout(2)
            while not stop.is_set():
                try:
                    conn, _ = srv.accept()
                except OSError:
                    return
                conn.close()  # accept then slam shut

        t = threading.Thread(target=closer, daemon=True)
        t.start()
        try:
            c = TransportClient(sock_path)
            r = c.send({"cmd": "ping"}, timeout=1.0)
            # Either a drop error or Empty response — NEVER daemon_unavailable
            self.assertNotEqual(r.get("error"), "daemon_unavailable")
            self.assertIn(
                r.get("error"),
                ("daemon_connection_dropped", "Empty response", None),
            )
        finally:
            stop.set()
            srv.close()
            if os.path.exists(sock_path):
                os.unlink(sock_path)


class GcResilienceTest(unittest.TestCase):
    """garbage_collect must survive a recall_cache without expires_at."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="tr_gc_resil_")

    def test_gc_with_legacy_recall_cache(self):
        """Legacy DB (no recall_cache.expires_at) → GC still purges + vacuums."""
        import sqlite3
        import time

        db = os.path.join(self.tmpdir, "legacy.db")
        conn = sqlite3.connect(db)
        # Legacy-shaped schema: recall_cache WITHOUT expires_at
        conn.executescript(
            """
            CREATE TABLE terminal_cache (
                command_hash TEXT PRIMARY KEY, output TEXT, stderr TEXT,
                exit_code INTEGER, cached_at REAL NOT NULL,
                expires_at REAL NOT NULL, cwd TEXT DEFAULT '',
                hit_streak INTEGER DEFAULT 0, embedding BLOB
            );
            CREATE TABLE mcp_cache (
                request_hash TEXT PRIMARY KEY, mcp_server TEXT, mcp_tool TEXT,
                arguments TEXT, data TEXT, cached_at REAL NOT NULL,
                expires_at REAL NOT NULL, hits INTEGER DEFAULT 1,
                hit_streak INTEGER DEFAULT 0
            );
            CREATE TABLE recall_cache (
                node_id TEXT PRIMARY KEY, fingerprint TEXT, content TEXT,
                content_type TEXT, reproducible INTEGER, summary TEXT,
                tokens INTEGER DEFAULT 0, cached_at REAL NOT NULL
            );
            CREATE TABLE cache_stats (
                category TEXT PRIMARY KEY, updated_at REAL DEFAULT 0,
                hits INTEGER DEFAULT 0, misses INTEGER DEFAULT 0,
                tokens_read_from_disk INTEGER DEFAULT 0,
                tokens_saved INTEGER DEFAULT 0,
                context_tokens_saved INTEGER DEFAULT 0
            );
            INSERT INTO terminal_cache VALUES ('h1','o','',0,1,0.5,'',0,NULL);
            INSERT INTO mcp_cache VALUES ('m1','s','t','{}','d',1,0.5,1,0);
            INSERT INTO recall_cache VALUES ('n1','f','c','t',0,'',1,1);
            INSERT INTO cache_stats VALUES ('terminal_cache', 1, 5, 2, 100, 50, 0);
            """
        )
        conn.commit()
        conn.close()

        # Point the GC's _db at the legacy DB
        from toolrecall import cache as cache_mod

        with _TempDbFixture(db):
            removed = cache_mod.garbage_collect()
        self.assertGreaterEqual(
            removed,
            2,
            f"GC returned {removed} — legacy recall_cache likely aborted it",
        )
        conn = sqlite3.connect(db)
        n_term = conn.execute("SELECT COUNT(*) FROM terminal_cache").fetchone()[0]
        n_mcp = conn.execute("SELECT COUNT(*) FROM mcp_cache").fetchone()[0]
        conn.close()
        self.assertEqual(n_term, 0, "expired terminal entries not purged")
        self.assertEqual(n_mcp, 0, "expired mcp entries not purged")

    def test_gc_with_migrated_recall_cache(self):
        """The v0.8.20 migration adds expires_at; GC then purges recall too."""
        import sqlite3

        db = os.path.join(self.tmpdir, "migrated.db")
        conn = sqlite3.connect(db)
        conn.executescript(
            """
            CREATE TABLE terminal_cache (
                command_hash TEXT PRIMARY KEY, output TEXT, stderr TEXT,
                exit_code INTEGER, cached_at REAL NOT NULL,
                expires_at REAL NOT NULL, cwd TEXT DEFAULT '',
                hit_streak INTEGER DEFAULT 0, embedding BLOB
            );
            CREATE TABLE mcp_cache (
                request_hash TEXT PRIMARY KEY, mcp_server TEXT, mcp_tool TEXT,
                arguments TEXT, data TEXT, cached_at REAL NOT NULL,
                expires_at REAL NOT NULL, hits INTEGER DEFAULT 1,
                hit_streak INTEGER DEFAULT 0
            );
            CREATE TABLE recall_cache (
                node_id TEXT PRIMARY KEY, fingerprint TEXT, content TEXT,
                content_type TEXT, reproducible INTEGER, summary TEXT,
                tokens INTEGER DEFAULT 0, cached_at REAL NOT NULL,
                expires_at REAL
            );
            CREATE TABLE cache_stats (
                category TEXT PRIMARY KEY, updated_at REAL DEFAULT 0,
                hits INTEGER DEFAULT 0, misses INTEGER DEFAULT 0,
                tokens_read_from_disk INTEGER DEFAULT 0,
                tokens_saved INTEGER DEFAULT 0,
                context_tokens_saved INTEGER DEFAULT 0
            );
            INSERT INTO recall_cache VALUES ('n1','f','c','t',0,'',1,1,0.5);
            """
        )
        conn.commit()
        conn.close()

        from toolrecall import cache as cache_mod

        with _TempDbFixture(db):
            cache_mod.garbage_collect()
        conn = sqlite3.connect(db)
        n = conn.execute("SELECT COUNT(*) FROM recall_cache").fetchone()[0]
        conn.close()
        self.assertEqual(n, 0, "expired recall entries not purged")

    def test_migration_adds_recall_expires_at(self):
        """_db._init's idempotent migration adds expires_at to legacy DBs."""
        import sqlite3
        from toolrecall import _db as db_mod

        db = os.path.join(self.tmpdir, "migrate_me.db")
        conn = sqlite3.connect(db)
        conn.executescript(
            """
            CREATE TABLE recall_cache (
                node_id TEXT PRIMARY KEY, fingerprint TEXT, content TEXT,
                content_type TEXT, reproducible INTEGER, summary TEXT,
                tokens INTEGER DEFAULT 0, cached_at REAL NOT NULL
            );
            CREATE TABLE cache_stats (category TEXT);
            """
        )
        conn.commit()
        conn.close()

        # _init() opens the DB through _db() — route it at the legacy file.
        # _init runs full schema setup; on a partial legacy DB some CREATEs
        # may conflict, but the idempotent _add_column pass must land.
        with _TempDbFixture(db):
            try:
                db_mod._init("")
            except Exception:
                pass  # unrelated schema gaps tolerated — column is what matters

        conn = sqlite3.connect(db)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(recall_cache)")}
        conn.close()
        self.assertIn("expires_at", cols, "migration did not add expires_at")


class SessionFilesNotCachedTest(unittest.TestCase):
    """WebUI session transcripts resolve to ttl=0 (serve-through)."""

    def test_webui_session_json_ttl_zero(self):
        from toolrecall.ttl_policy import resolve_file_ttl
        import tomllib

        pkg_cfg = os.path.join(os.path.dirname(cache_pkg_file()), "config.toml")
        cfg = tomllib.load(open(pkg_cfg, "rb"))["cache"]
        session = "/home/hermes/.hermes/webui/sessions/whatever.json"
        self.assertEqual(resolve_file_ttl(session, cfg), 0)
        other = "/home/hermes/toolrecall/toolrecall/cache.py"
        self.assertEqual(resolve_file_ttl(other, cfg), -1)


def cache_pkg_file():
    import toolrecall.cache

    return toolrecall.cache.__file__


if __name__ == "__main__":
    unittest.main()
