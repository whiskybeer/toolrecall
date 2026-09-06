"""TTL boundary tests for the recall tier (toolrecall/recall.py).

Existing TTL coverage in test_recall.py uses wide tolerances (±100s offsets,
delta=5). These tests pin the exact boundary semantics:

  * expiry comparison is strict: expires_at == now must still be LIVE
    (``row[5] < time.time()`` — equal is not expired);
  * 1ms after expiry the entry is a miss AND is lazily purged;
  * sub-second TTLs work (stored expires_at is float, not truncated to int);
  * purge_expired() uses the same strict comparison as get();
  * a negative/zero TTL stores a never-expiring entry (not an immediate
    expiry);
  * clock-skew: an entry whose expires_at is already in the past at store
    time is a miss on first get, never served.

Standalone-runnable: sets/restores TOOLRECALL_CACHE_DB itself.
"""

import os
import shutil
import tempfile
import time
import unittest


class TestRecallTTLBoundaries(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.mkdtemp(prefix="tr_ttl_")
        os.environ["TOOLRECALL_CACHE_DB"] = os.path.join(cls._tmp, "ttl.db")
        import toolrecall._db as _db

        # Close any shared singleton so it reopens against the temp path
        with _db._db_lock:
            if _db._db_real is not None:
                _db._db_real.close()
            _db._db_real = None
            _db._db_path_cached = None
            _db._cached_config = None
        from toolrecall.cache import _init

        _init()

    @classmethod
    def tearDownClass(cls):
        import toolrecall._db as _db

        with _db._db_lock:
            if _db._db_real is not None:
                _db._db_real.close()
            _db._db_real = None
            _db._db_path_cached = None
            _db._cached_config = None
        os.environ.pop("TOOLRECALL_CACHE_DB", None)
        shutil.rmtree(cls._tmp, ignore_errors=True)

    # ── helpers ──────────────────────────────────────────────

    def _expires_at(self, node_id_):
        from toolrecall._db import _db as _db_call

        with _db_call() as conn:
            row = conn.execute(
                "SELECT expires_at FROM recall_cache WHERE node_id=?", (node_id_,)
            ).fetchone()
        return row[0] if row else None

    def _store(self, fp, ttl=None, expires_at_override=None):
        from toolrecall import recall
        from toolrecall._db import _db as _db_call

        nid = recall.store(
            fingerprint=fp,
            content="boundary-content",
            content_type="web",
            reproducible=False,
            ttl=ttl if ttl is not None else 0,
        )
        if expires_at_override is not None:
            with _db_call() as conn:
                conn.execute(
                    "UPDATE recall_cache SET expires_at=? WHERE node_id=?",
                    (expires_at_override, nid),
                )
        return nid

    # ── exact boundary ───────────────────────────────────────

    def test_expiry_equal_to_now_is_still_live(self):
        """expires_at == now must be served (strict < comparison).

        time.time() is frozen so the boundary is exact — wall clock would
        advance between the UPDATE and the assertion, making the boundary
        untestable.
        """
        import unittest.mock as mock

        from toolrecall import recall

        nid = recall.store(
            fingerprint="eq-now", content="x", content_type="web", reproducible=False
        )
        now = 1_800_000_000.0  # fixed epoch so expires_at == now exactly
        with self._db() as conn:
            conn.execute("UPDATE recall_cache SET expires_at=? WHERE node_id=?", (now, nid))
        with mock.patch("toolrecall.recall.time.time", return_value=now):
            self.assertIsNotNone(
                recall.get(nid), "expires_at == now must NOT be treated as expired"
            )

    def test_expiry_one_ms_in_past_is_miss_and_purged(self):
        """1ms past expiry: miss + lazy purge (row gone from the table)."""
        from toolrecall import recall

        nid = recall.store(
            fingerprint="plus-1ms", content="x", content_type="web", reproducible=False
        )
        with self._db() as conn:
            conn.execute(
                "UPDATE recall_cache SET expires_at=? WHERE node_id=?",
                (time.time() - 0.001, nid),
            )
        before = self._count()
        self.assertIsNone(recall.get(nid))
        self.assertEqual(self._count(), before - 1, "lazy purge didn't delete")

    def test_sub_second_ttl_roundtrip(self):
        """ttl=0.05: still live immediately, gone after 60ms sleep."""
        from toolrecall import recall

        nid = recall.store(
            fingerprint="subsec",
            content="x",
            content_type="web",
            reproducible=False,
            ttl=0.05,
        )
        exp = self._expires_at(nid)
        self.assertIsNotNone(exp)
        # expires_at must be float-precise, not truncated to an int epoch
        self.assertGreater(exp - time.time(), 0.04, "TTL truncated below 50ms")
        self.assertIsNotNone(recall.get(nid), "should be live within TTL window")
        time.sleep(0.06)
        self.assertIsNone(recall.get(nid), "should be expired 60ms in")

    def test_zero_and_negative_ttl_mean_never_expire(self):
        """ttl=0 and negative must store NULL expires_at (never expire),
        not an instantly-expired row."""
        from toolrecall import recall

        for ttl in (0, -5.0):
            nid = recall.store(
                fingerprint=f"never-{ttl}",
                content="x",
                content_type="web",
                reproducible=False,
                ttl=ttl,
            )
            self.assertIsNone(self._expires_at(nid), f"ttl={ttl} must mean never-expire")
            self.assertIsNotNone(recall.get(nid))

    def test_purge_uses_same_strict_comparison(self):
        """purge_expired() and get() must agree at the boundary: a row with
        expires_at exactly == now survives BOTH; 1us past is swept by BOTH."""
        from toolrecall import recall

        nid = recall.store(
            fingerprint="purge-b", content="x", content_type="web", reproducible=False
        )
        import unittest.mock as mock

        now = 1_800_000_000.0  # fixed epoch: exact boundary, no clock race
        with self._db() as conn:
            conn.execute("UPDATE recall_cache SET expires_at=? WHERE node_id=?", (now, nid))
        with mock.patch("toolrecall.recall.time.time", return_value=now):
            self.assertEqual(recall.purge_expired(), 0, "== now must survive purge")
            self.assertIsNotNone(recall.get(nid))

        with self._db() as conn:
            conn.execute(
                "UPDATE recall_cache SET expires_at=? WHERE node_id=?",
                (now - 0.001, nid),
            )
        with mock.patch("toolrecall.recall.time.time", return_value=now):
            self.assertEqual(recall.purge_expired(), 1, "1ms past must be swept")
            self.assertIsNone(recall.get(nid))

    def test_clock_skew_expiry_in_past_at_store_time(self):
        """expires_at already in the past when stored: first get is a miss,
        never stale bytes."""
        from toolrecall import recall

        nid = recall.store(fingerprint="skew", content="x", content_type="web", reproducible=False)
        # Simulate a writer whose clock was 60s ahead
        with self._db() as conn:
            conn.execute(
                "UPDATE recall_cache SET expires_at=? WHERE node_id=?",
                (time.time() - 60, nid),
            )
        before = self._count()
        self.assertIsNone(recall.get(nid), "past-dated entry must never be served")
        self.assertEqual(self._count(), before - 1)

    # ── plumbing ─────────────────────────────────────────────

    def _db(self):
        from toolrecall._db import _db as _db_call

        return _db_call()

    def _count(self) -> int:
        with self._db() as conn:
            return conn.execute("SELECT COUNT(*) FROM recall_cache").fetchone()[0]


if __name__ == "__main__":
    unittest.main()
