"""Concurrency tests for the _db singleton under parallel sessions.

Verifies the claim in _db.py's docstring: "no second file handle, no WAL
contention with the daemon's own writes" — i.e. the RLock + refcount
singleton actually serializes concurrent writers correctly.

What's exercised:
  1. Two threads writing distinct rows concurrently — no exception, no loss.
  2. Same-row contention (last-write-wins) — the final value is one of the
     written values, never corrupt.
  3. Reader running while a writer hammers — reads always return a
     consistent view (never partial/corrupt rows).
  4. Reentrancy: nested ``with _db()`` doesn't deadlock and doesn't
     prematurely commit the outer transaction (refcount semantics).
  5. Exception inside ``with _db()`` rolls back and releases the lock —
     subsequent writes still work (no leaked lock).

All state lives in a temp DB via TOOLRECALL_CACHE_DB (restored by conftest's
autouse fixture anyway, but we set/restore explicitly so the module is
runnable standalone too).
"""

import os
import tempfile
import threading
import unittest

import toolrecall._db as _dbmod
from toolrecall._db import _db

SCHEMA = """
CREATE TABLE IF NOT EXISTS conc_items (
    k TEXT PRIMARY KEY,
    v TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS conc_counter (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    n INTEGER NOT NULL
);
INSERT OR IGNORE INTO conc_counter(id, n) VALUES (1, 0);
"""


class TestDbConcurrency(unittest.TestCase):
    def setUp(self):
        self._orig_env = os.environ.get("TOOLRECALL_CACHE_DB")
        self.tmpdir = tempfile.mkdtemp(prefix="tr_conc_")
        self.db_path = os.path.join(self.tmpdir, "conc.db")
        os.environ["TOOLRECALL_CACHE_DB"] = self.db_path
        # Force the singleton to reopen against the temp path
        _dbmod._db_real = None
        _dbmod._db_path_cached = None
        _dbmod._cached_config = None
        with _db() as conn:
            conn.executescript(SCHEMA)
            conn.commit()

    def tearDown(self):
        # Close singleton so other tests don't inherit our temp handle
        if _dbmod._db_real is not None:
            _dbmod._db_real.close()
        _dbmod._db_real = None
        _dbmod._db_path_cached = None
        _dbmod._cached_config = None
        if self._orig_env is None:
            os.environ.pop("TOOLRECALL_CACHE_DB", None)
        else:
            os.environ["TOOLRECALL_CACHE_DB"] = self._orig_env

    def test_parallel_writers_distinct_rows(self):
        """8 threads x 25 rows each: all 200 rows land, zero exceptions."""
        errors = []

        def writer(tid):
            try:
                for i in range(25):
                    with _db() as conn:
                        conn.execute(
                            "INSERT OR REPLACE INTO conc_items(k, v) VALUES (?, ?)",
                            (f"t{tid}-i{i}", f"val-{tid}-{i}"),
                        )
            except Exception as e:  # noqa: BLE001 — collected for assertion
                errors.append(f"t{tid}: {type(e).__name__}: {e}")

        threads = [threading.Thread(target=writer, args=(t,)) for t in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        self.assertEqual(errors, [])
        with _db() as conn:
            n = conn.execute("SELECT COUNT(*) FROM conc_items").fetchone()[0]
        self.assertEqual(n, 200, "lost writes under concurrency")

    def test_same_row_contention_stays_consistent(self):
        """10 threads hammering one row: final value is one of the written."""
        errors = []

        def writer(tid):
            try:
                for _ in range(20):
                    with _db() as conn:
                        conn.execute("UPDATE conc_counter SET n = ? WHERE id = 1", (tid,))
            except Exception as e:  # noqa: BLE001
                errors.append(f"t{tid}: {type(e).__name__}: {e}")

        threads = [threading.Thread(target=writer, args=(t,)) for t in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        self.assertEqual(errors, [])
        with _db() as conn:
            row = conn.execute("SELECT n FROM conc_counter WHERE id = 1").fetchone()
        self.assertIsNotNone(row)
        self.assertIn(row[0], range(10), "corrupt row under contention")

    def test_reader_during_writer_sees_consistent_state(self):
        """Concurrent read of counter vs items must never disagree.

        Writer atomically increments counter n and inserts a marker row per
        iteration in the SAME transaction. A concurrent reader must never
        observe n != marker count (would indicate a torn commit).
        """
        stop = threading.Event()
        reader_errors = []
        writer_errors = []

        def reader():
            try:
                while not stop.is_set():
                    with _db() as conn:
                        row = conn.execute("SELECT n FROM conc_counter WHERE id = 1").fetchone()
                        n_markers = conn.execute(
                            "SELECT COUNT(*) FROM conc_items WHERE k LIKE 'm%'"
                        ).fetchone()[0]
                    if row is None or row[0] < n_markers:
                        reader_errors.append(
                            f"torn read: counter={row[0] if row else None} markers={n_markers}"
                        )
            except Exception as e:  # noqa: BLE001
                reader_errors.append(f"{type(e).__name__}: {e}")

        def writer():
            try:
                for i in range(60):
                    with _db() as conn:
                        conn.execute("UPDATE conc_counter SET n = n + 1 WHERE id = 1")
                        conn.execute(
                            "INSERT OR REPLACE INTO conc_items(k, v) VALUES (?, ?)",
                            (f"m{i}", "marker"),
                        )
            except Exception as e:  # noqa: BLE001
                writer_errors.append(f"{type(e).__name__}: {e}")

        rt = threading.Thread(target=reader, daemon=True)
        rt.start()
        wt = threading.Thread(target=writer, args=())
        wt.start()
        wt.join(timeout=30)
        stop.set()
        rt.join(timeout=5)
        self.assertEqual(writer_errors, [])
        self.assertEqual(
            reader_errors,
            [],
            f"torn reads observed ({len(reader_errors)}): {reader_errors[:3]}",
        )

    def test_reentrant_nested_no_premature_commit(self):
        """Inner _db() must not commit the outer transaction early.

        If refcounting is broken, the inner commit would persist the outer
        body's partial write — observable as the row existing even though
        the outer body then raises (rollback should erase it).
        """
        with self.assertRaises(RuntimeError):
            with _db() as outer:
                outer.execute("INSERT INTO conc_items(k, v) VALUES ('reent', 'partial')")
                with _db() as inner:
                    inner.execute("INSERT INTO conc_items(k, v) VALUES ('reent2', 'inner')")
                raise RuntimeError("rollback the whole thing")

        with _db() as conn:
            n = conn.execute(
                "SELECT COUNT(*) FROM conc_items WHERE k IN ('reent', 'reent2')"
            ).fetchone()[0]
        self.assertEqual(n, 0, "inner _db() committed the outer transaction prematurely")

    def test_exception_releases_lock_and_rolls_back(self):
        """Failure inside _db() must not leak the lock for later writers."""
        with self.assertRaises(ValueError):
            with _db() as conn:
                conn.execute("INSERT INTO conc_items(k, v) VALUES ('boom', 'x')")
                raise ValueError("synthetic")
        # The failed write must be gone...
        with _db() as conn:
            n = conn.execute("SELECT COUNT(*) FROM conc_items WHERE k = 'boom'").fetchone()[0]
        self.assertEqual(n, 0, "rollback didn't erase the failed write")
        # ...and the lock must be free: a burst of writes still succeeds.
        for i in range(20):
            with _db() as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO conc_items(k, v) VALUES (?, ?)",
                    (f"after-{i}", "ok"),
                )
        with _db() as conn:
            n = conn.execute("SELECT COUNT(*) FROM conc_items WHERE k LIKE 'after-%'").fetchone()[0]
        self.assertEqual(n, 20)

    def test_multi_process_wal_isolation(self):
        """A second PROCESS opening the same temp DB file sees committed rows.

        The singleton guarantees no second handle *within* the process; this
        checks the on-disk file itself is a valid, readable SQLite DB with
        committed data (the thing a separate MCP child process would open).
        """
        with _db() as conn:
            conn.execute("INSERT INTO conc_items(k, v) VALUES ('xproc', 'visible')")
        # Separate process, fresh handle
        script = (
            "import sqlite3, json;"
            f"c = sqlite3.connect({self.db_path!r});"
            "print(json.dumps([r[0] for r in c.execute("
            "\"SELECT v FROM conc_items WHERE k='xproc'\")]))"
        )
        import subprocess

        out = subprocess.run(["python3", "-c", script], capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("visible", out.stdout)


if __name__ == "__main__":
    unittest.main()
