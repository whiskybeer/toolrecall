"""B2 pinning tests: daemon GC purges expired recall_cache rows.

recall.purge_expired() has no prod caller; the GC that actually purges
expired recall entries is cache.garbage_collect() (daemon thread
_run_periodic_gc every 4h), which inline-DELETEs
recall_cache.expires_at < now alongside terminal_cache/mcp_cache. The
dead-code audit flagged purge_expired as prod-dead; the product decision
(recorded 2026-09-13) was: keep the inline GC purge as the wired path, keep
purge_expired as the recall-tier public API (same pattern as cached_exec /
cached_run), and pin the GC behavior so a refactor can't silently drop it.

Pins:
  * garbage_collect() deletes expired recall rows and keeps live ones;
  * the returned count includes deleted recall rows;
  * the strict comparison (expires_at == now is still live) matches get().

get()'s own boundary strictness is pinned separately in
test_recall_ttl_boundary.py; the GC boundary here is deterministic via a
frozen clock (garbage_collect() does its own `import time`, so the patch
must hit the time module, not cache_mod.time).

Standalone-runnable; sets/restores TOOLRECALL_CACHE_DB itself.
"""

import os
import shutil
import tempfile
import time
import unittest


class TestGCPurgesRecall(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.mkdtemp(prefix="tr_gc_recall_")
        cls._db_path = os.path.join(cls._tmp, "gc.db")
        os.environ["TOOLRECALL_CACHE_DB"] = cls._db_path  # standalone-mode pin

    def setUp(self):
        # conftest's autouse fixture force-pins a FRESH per-test DB AFTER
        # setUpClass ran (8c4db91); re-assert the class pin so the
        # one-DB-per-class design — and _init()'s schema — live in the SAME
        # file the tests query. Standalone runs skip conftest and already
        # hold the class path. Singleton must be closed so it reopens
        # against the class path; _init() is idempotent.
        os.environ["TOOLRECALL_CACHE_DB"] = self._db_path
        import toolrecall._db as _db

        with _db._db_lock:
            if _db._db_real is not None:
                _db._db_real.close()
            _db._db_real = None
            _db._db_path_cached = None
            _db._cached_config = None
        from toolrecall.cache import _init

        _init()

    def tearDown(self):
        # Close the singleton so the next test's env pin takes effect.
        import toolrecall._db as _db

        with _db._db_lock:
            if _db._db_real is not None:
                _db._db_real.close()
            _db._db_real = None
            _db._db_path_cached = None
            _db._cached_config = None

    @classmethod
    def tearDownClass(cls):
        os.environ.pop("TOOLRECALL_CACHE_DB", None)
        shutil.rmtree(cls._tmp, ignore_errors=True)

    def _store(self, fp, ttl):
        from toolrecall import recall

        return recall.store(
            fingerprint=fp,
            content="gc-pin-content",
            content_type="web",
            reproducible=False,
            ttl=ttl,
        )

    def _recall_rows(self):
        from toolrecall._db import _db as _db_call

        with _db_call() as conn:
            return conn.execute("SELECT COUNT(*) FROM recall_cache").fetchone()[0]

    def test_gc_deletes_expired_keeps_live(self):
        from toolrecall.cache import garbage_collect

        rows_before = self._recall_rows()
        expired_id = self._store("gc-expired", ttl=0.01)
        live_id = self._store("gc-live", ttl=3600)
        forever_id = self._store("gc-forever", ttl=0)  # never expires
        time.sleep(0.03)

        deleted = garbage_collect()

        self.assertIsInstance(deleted, int)
        self.assertGreaterEqual(deleted, 1)
        from toolrecall._db import _db as _db_call

        with _db_call() as conn:
            for nid, alive in ((expired_id, False), (live_id, True), (forever_id, True)):
                row = conn.execute("SELECT 1 FROM recall_cache WHERE node_id=?", (nid,)).fetchone()
                self.assertEqual(row is not None, alive, f"node {nid[:8]} presence != {alive}")
        # Exactly one recall row removed, regardless of cross-test leftovers.
        self.assertEqual(self._recall_rows(), rows_before + 3 - 1)

    def test_gc_boundary_strict_comparison(self):
        """GC keeps a row whose expires_at == now (strict <), deletes past it.

        Deterministic via a frozen clock. NOTE: garbage_collect() does its
        own ``import time``, so patching cache_mod.time is ineffective —
        the patch must hit the time module itself (sys.modules['time']),
        scoped and restored in finally.
        """
        from toolrecall._db import _db as _db_call
        import sys

        nid = self._store("gc-boundary", ttl=10.0)
        with _db_call() as conn:
            expires = conn.execute(
                "SELECT expires_at FROM recall_cache WHERE node_id=?", (nid,)
            ).fetchone()[0]

        real_time = sys.modules["time"].time
        try:
            # Pass 1: GC now == expires_at -> strict < keeps the row.
            sys.modules["time"].time = lambda: expires
            from toolrecall.cache import garbage_collect

            garbage_collect()
            with _db_call() as conn:
                still = conn.execute(
                    "SELECT 1 FROM recall_cache WHERE node_id=?", (nid,)
                ).fetchone()
            self.assertIsNotNone(still, "GC deleted a row whose expires_at == now")

            # Pass 2: GC now strictly past expiry -> row removed.
            sys.modules["time"].time = lambda: expires + 0.01
            garbage_collect()
            with _db_call() as conn:
                gone = conn.execute("SELECT 1 FROM recall_cache WHERE node_id=?", (nid,)).fetchone()
            self.assertIsNone(gone)
        finally:
            sys.modules["time"].time = real_time


if __name__ == "__main__":
    unittest.main()
