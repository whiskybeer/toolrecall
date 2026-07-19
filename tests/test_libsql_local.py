#!/usr/bin/env python3
"""Test the libSQL backend locally -- creates a temp DB, runs schema init, CRUD, cursor wrappers.

Usage: python3 tests/test_libsql_local.py
"""

import os
import sys
import tempfile
import unittest

# Ensure toolrecall is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Skip the whole class when the optional dependency is missing (works
# under both `python -m unittest` and pytest). Crucially, this module
# does NOT mutate os.environ at import time -- that would leak
# backend=libsql into every other test in the same session.
try:
    import libsql_experimental  # noqa: F401
    _HAS_LIBSQL = True
except ImportError:
    _HAS_LIBSQL = False

from toolrecall._db import _open_db, _LibSQLConnection, _LibSQLCursor, _LibSQLRow
from toolrecall.config import load_config


@unittest.skipUnless(_HAS_LIBSQL, "libsql-experimental not installed")
class TestLibSQLLocal(unittest.TestCase):
    """Test libSQL backend against a real libsql DB file."""

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.mkdtemp(prefix="tr_libsql_test_")
        cls.db_path = os.path.join(cls.tmpdir, "test_libsql.db")
        # Override config so _open_db uses our path.
        # Env is mutated ONLY inside setUpClass and fully restored in
        # tearDownClass -- never at module import.
        cls._orig_environ = dict(os.environ)
        os.environ["TOOLRECALL_STORAGE_BACKEND"] = "libsql"
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

    # --- Extended tests for full coverage ---

    def test_12_executescript(self):
        """executescript() runs multiple statements."""
        conn = _open_db(self.cfg)
        conn.executescript(
            "CREATE TABLE t1 (a INTEGER);"
            "CREATE TABLE t2 (b TEXT);"
            "INSERT INTO t1 VALUES (1);"
            "INSERT INTO t2 VALUES ('x');"
        )
        conn.commit()
        rows = conn.execute("SELECT a FROM t1").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["a"], 1)
        conn.close()

    def test_13_column_name_casing(self):
        """Column names preserve original casing for string-key access."""
        conn = _open_db(self.cfg)
        conn.execute("CREATE TABLE t (MixedCase TEXT, UPPER INTEGER)")
        conn.execute("INSERT INTO t VALUES ('hello', 42)")
        conn.commit()
        row = conn.execute("SELECT * FROM t").fetchone()
        self.assertEqual(row["MixedCase"], "hello")
        self.assertEqual(row["UPPER"], 42)
        self.assertRaises(KeyError, lambda: row["mixedcase"])
        conn.close()

    def test_14_fetchmany(self):
        """fetchmany() returns a limited batch of _LibSQLRow objects."""
        conn = _open_db(self.cfg)
        conn.execute("CREATE TABLE t (v INTEGER)")
        for i in range(10):
            conn.execute("INSERT INTO t VALUES (?)", (i,))
        conn.commit()
        cur = conn.execute("SELECT v FROM t ORDER BY v")
        batch = cur.fetchmany(3)
        self.assertEqual(len(batch), 3)
        self.assertEqual(batch[0]["v"], 0)
        self.assertEqual(batch[2]["v"], 2)
        remaining = cur.fetchall()
        self.assertEqual(len(remaining), 7)
        conn.close()

    def test_15_iter_empty_cursor(self):
        """Iterating over an empty cursor yields nothing (no StopIteration crash)."""
        conn = _open_db(self.cfg)
        conn.execute("CREATE TABLE t (v INTEGER)")
        conn.commit()
        vals = [row["v"] for row in conn.execute("SELECT v FROM t")]
        self.assertEqual(vals, [])
        conn.close()

    def test_16_repr_tuple_equivalence(self):
        """__repr__ output matches tuple repr of row values."""
        conn = _open_db(self.cfg)
        conn.execute("CREATE TABLE t (x INTEGER, y TEXT)")
        conn.execute("INSERT INTO t VALUES (10, 'abc')")
        conn.commit()
        row = conn.execute("SELECT * FROM t").fetchone()
        self.assertEqual(repr(row), "(10, 'abc')")
        conn.close()

    def test_17_null_and_blob_roundtrip(self):
        """NULL and BLOB values round-trip correctly."""
        conn = _open_db(self.cfg)
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, blob_val BLOB, null_val TEXT)")
        conn.execute("INSERT INTO t VALUES (1, X'001122', NULL)")
        conn.execute("INSERT INTO t VALUES (2, NULL, 'not null')")
        conn.commit()
        row1 = conn.execute("SELECT * FROM t WHERE id = 1").fetchone()
        self.assertIsNone(row1["null_val"])
        self.assertEqual(row1["blob_val"], b"\x00\x11\"")
        row2 = conn.execute("SELECT * FROM t WHERE id = 2").fetchone()
        self.assertIsNone(row2["blob_val"])
        self.assertEqual(row2["null_val"], "not null")
        conn.close()

    def test_18_cursor_description(self):
        """Cursor.description returns column metadata."""
        conn = _open_db(self.cfg)
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, label TEXT NOT NULL)")
        conn.commit()
        cur = conn.execute("SELECT id, label FROM t")
        desc = cur.description
        self.assertIsNotNone(desc)
        names = [d[0] for d in desc]
        self.assertEqual(names, ["id", "label"])
        conn.close()

    def test_19_large_string(self):
        """Large string values store and retrieve correctly."""
        conn = _open_db(self.cfg)
        conn.execute("CREATE TABLE t (v TEXT)")
        big = "x" * 100_000
        conn.execute("INSERT INTO t VALUES (?)", (big,))
        conn.commit()
        row = conn.execute("SELECT v FROM t").fetchone()
        self.assertEqual(len(row["v"]), 100_000)
        self.assertEqual(row["v"], big)
        conn.close()

    def test_20_total_changes_noop_read(self):
        """SELECT does NOT increment total_changes."""
        conn = _open_db(self.cfg)
        conn.execute("CREATE TABLE t (v INTEGER)")
        conn.execute("INSERT INTO t VALUES (1)")
        conn.commit()
        before = conn.total_changes
        conn.execute("SELECT * FROM t")
        self.assertEqual(conn.total_changes, before)
        conn.close()

    def test_21_stats_info(self):
        """stats_info() returns expected keys for libsql backend."""
        from toolrecall.storage.libsql import stats_info
        info = stats_info(self.cfg)
        self.assertIn("sync_enabled", info)
        self.assertIn("sync_url", info)
        self.assertIn("sync_interval", info)
        self.assertFalse(info["sync_enabled"])  # default off

    def test_22_sync_configured_local(self):
        """sync_configured() is False without sync_url/token (pure local)."""
        from toolrecall.storage.libsql import sync_configured
        self.assertFalse(sync_configured(self.cfg))

    def test_23_sync_configured_full_opt_in(self):
        """sync_configured() requires all three: enabled + url + token."""
        from toolrecall.storage.libsql import sync_configured
        from toolrecall.config import load_config

        os.environ["TOOLRECALL_SYNC_ENABLED"] = "true"
        os.environ["TOOLRECALL_SYNC_URL"] = "libsql://x.turso.io"
        os.environ["TOOLRECALL_SYNC_TOKEN"] = "tok"
        cfg2 = load_config()
        self.assertTrue(sync_configured(cfg2))
        os.environ.pop("TOOLRECALL_SYNC_ENABLED")

    def test_24_active_db_path_libsql(self):
        """active_db_path() returns the libsql-specific path when backend=libsql."""
        from toolrecall.storage import active_db_path
        self.assertTrue(active_db_path(self.cfg).endswith("test_libsql.db"))


if __name__ == "__main__":
    unittest.main(verbosity=2)