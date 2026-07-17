#!/usr/bin/env python3
"""Test the libSQL backend locally — creates a temp DB, runs schema init, CRUD, cursor wrappers.

Usage: python3 tests/test_libsql_local.py
"""

import os
import sys
import tempfile
import unittest

# Ensure toolrecall is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Force libSQL backend via env
os.environ["TOOLRECALL_STORAGE_BACKEND"] = "libsql"

from toolrecall._db import _open_db, _LibSQLConnection, _LibSQLCursor, _LibSQLRow
from toolrecall.config import load_config


class TestLibSQLLocal(unittest.TestCase):
    """Test libSQL backend against a real libsql DB file."""

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.mkdtemp(prefix="tr_libsql_test_")
        cls.db_path = os.path.join(cls.tmpdir, "test_libsql.db")
        # Override config so _open_db uses our path
        cls._orig_environ = dict(os.environ)
        os.environ["TOOLRECALL_CACHE_DB"] = cls.db_path
        os.environ["TOOLRECALL_LIBSQL_DB_PATH"] = cls.db_path

    @classmethod
    def tearDownClass(cls):
        os.environ.clear()
        os.environ.update(cls._orig_environ)
        import shutil
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def setUp(self):
        # Clean DB file for each test
        if os.path.exists(self.db_path):
            os.remove(self.db_path)
        # Also remove WAL/SHM
        for ext in ("-wal", "-shm"):
            p = self.db_path + ext
            if os.path.exists(p):
                os.remove(p)
        self.cfg = load_config()

    def test_01_open_db_returns_libsql_connection(self):
        """_open_db() with libsql backend returns a _LibSQLConnection."""
        conn = _open_db(self.cfg)
        self.assertIsInstance(conn, _LibSQLConnection)
        conn.close()

    def test_02_create_table_and_insert(self):
        """Basic CREATE TABLE + INSERT + SELECT works."""
        conn = _open_db(self.cfg)
        conn.execute("CREATE TABLE test (id INTEGER PRIMARY KEY, name TEXT, value REAL)")
        conn.execute("INSERT INTO test (name, value) VALUES (?, ?)", ("foo", 1.23))
        conn.execute("INSERT INTO test (name, value) VALUES (?, ?)", ("bar", 4.56))
        conn.commit()

        rows = conn.execute("SELECT * FROM test ORDER BY id").fetchall()
        self.assertEqual(len(rows), 2)

        # Row by int index
        self.assertEqual(rows[0][0], 1)
        self.assertEqual(rows[0][1], "foo")
        self.assertAlmostEqual(rows[0][2], 1.23)

        # Row by string key
        self.assertEqual(rows[0]["name"], "foo")
        self.assertEqual(rows[1]["name"], "bar")

        conn.close()

    def test_03_fetchone(self):
        """fetchone() returns _LibSQLRow or None."""
        conn = _open_db(self.cfg)
        conn.execute("CREATE TABLE t (x INTEGER)")
        conn.execute("INSERT INTO t VALUES (42)")
        conn.commit()

        row = conn.execute("SELECT * FROM t").fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], 42)
        self.assertEqual(row["x"], 42)

        # No more rows
        none_row = conn.execute("SELECT * FROM t WHERE x = 999").fetchone()
        self.assertIsNone(none_row)

        conn.close()

    def test_04_cursor_iteration(self):
        """for row in cursor works (__iter__)."""
        conn = _open_db(self.cfg)
        conn.execute("CREATE TABLE t (v INTEGER)")
        for i in range(5):
            conn.execute("INSERT INTO t VALUES (?)", (i,))
        conn.commit()

        cursor = conn.execute("SELECT v FROM t ORDER BY v")
        vals = [row["v"] for row in cursor]
        self.assertEqual(vals, [0, 1, 2, 3, 4])

        conn.close()

    def test_05_total_changes(self):
        """total_changes tracks INSERT/UPDATE/DELETE row counts."""
        conn = _open_db(self.cfg)
        conn.execute("CREATE TABLE t (v INTEGER)")

        self.assertEqual(conn.total_changes, 0)

        conn.execute("INSERT INTO t VALUES (1)")
        self.assertGreater(conn.total_changes, 0)

        before = conn.total_changes
        conn.execute("INSERT INTO t VALUES (2)")
        self.assertGreater(conn.total_changes, before)

        conn.close()

    def test_06_row_factory_string_key(self):
        """Row supports both int and string key access (sqlite3.Row compat)."""
        conn = _open_db(self.cfg)
        conn.execute("CREATE TABLE t (a INTEGER, b TEXT, c REAL)")
        conn.execute("INSERT INTO t VALUES (1, 'hello', 3.14)")
        conn.commit()

        row = conn.execute("SELECT * FROM t").fetchone()
        self.assertEqual(row["a"], 1)
        self.assertEqual(row["b"], "hello")
        self.assertAlmostEqual(row["c"], 3.14)
        self.assertEqual(row[0], 1)
        self.assertEqual(row[1], "hello")
        self.assertAlmostEqual(row[2], 3.14)
        self.assertEqual(len(row), 3)

        conn.close()

    def test_07_executemany(self):
        """executemany works and updates total_changes."""
        conn = _open_db(self.cfg)
        conn.execute("CREATE TABLE t (v INTEGER)")
        conn.executemany("INSERT INTO t VALUES (?)", [(1,), (2,), (3,)])
        conn.commit()

        rows = conn.execute("SELECT COUNT(*) FROM t").fetchone()
        self.assertEqual(rows[0], 3)
        self.assertGreater(conn.total_changes, 0)

        conn.close()

    def test_08_context_manager(self):
        """_LibSQLConnection works as a context manager."""
        conn = _open_db(self.cfg)
        with conn:
            conn.execute("CREATE TABLE t (v INTEGER)")
            conn.execute("INSERT INTO t VALUES (42)")
        # Connection should be closed after context exit
        # (verifying it doesn't crash)

    def test_09_schema_init(self):
        """Run the full _init() schema creation."""
        from toolrecall.cache import SCHEMA
        from toolrecall._db import _init

        # Need to reload config with our temp path
        _init(schema=SCHEMA)

        # Verify tables exist
        conn = _open_db(self.cfg)
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        table_names = [r[0] for r in tables]
        self.assertIn("file_cache", table_names)
        self.assertIn("terminal_cache", table_names)
        self.assertIn("cache_stats", table_names)
        self.assertIn("script_cache", table_names)
        self.assertIn("code_cache", table_names)
        self.assertIn("mcp_cache", table_names)
        self.assertIn("browser_cache", table_names)
        self.assertIn("access_log", table_names)
        conn.close()

    def test_10_repr_works(self):
        """_LibSQLRow.__repr__ returns readable string."""
        conn = _open_db(self.cfg)
        conn.execute("CREATE TABLE t (x INTEGER, y TEXT)")
        conn.execute("INSERT INTO t VALUES (1, 'test')")
        conn.commit()

        row = conn.execute("SELECT * FROM t").fetchone()
        r = repr(row)
        self.assertIn("1", r)
        self.assertIn("test", r)

        conn.close()

    def test_11_rollback(self):
        """rollback() discards uncommitted changes."""
        conn = _open_db(self.cfg)
        conn.execute("CREATE TABLE t (v INTEGER)")
        conn.execute("INSERT INTO t VALUES (1)")
        conn.rollback()

        # Table exists but row was rolled back
        rows = conn.execute("SELECT COUNT(*) FROM t").fetchone()
        self.assertEqual(rows[0], 0)
        conn.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)