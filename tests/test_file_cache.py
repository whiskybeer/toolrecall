import os
import sys
import unittest
import tempfile
import time
import shutil

# Force a clean, isolated test database path before loading toolrecall
test_db_dir = tempfile.mkdtemp()
test_db_path = os.path.join(test_db_dir, "test_file_cache.db")
os.environ["TOOLRECALL_CACHE_DB"] = test_db_path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from toolrecall.cache import cached_read, _init


class TestFileCache(unittest.TestCase):
    def setUp(self):
        # Re-assert our DB path (other test modules may have changed it)
        os.environ["TOOLRECALL_CACHE_DB"] = test_db_path
        # Reset singleton DB connection so it re-opens with test DB path
        from toolrecall._db import _db_lock, _db_real
        import toolrecall._db as _db_mod

        _db_lock.acquire()
        if _db_real is not None:
            _db_real.close()
            _db_mod._db_real = None
        _db_lock.release()
        if os.path.exists(test_db_path):
            os.remove(test_db_path)
        _init()

        # Create a temporary file for reading
        self.fd, self.temp_file = tempfile.mkstemp(text=True)
        with os.fdopen(self.fd, "w") as f:
            f.write("line 1\nline 2\n")

    def tearDown(self):
        if os.path.exists(test_db_path):
            os.remove(test_db_path)
        if os.path.exists(self.temp_file):
            os.remove(self.temp_file)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(test_db_dir, ignore_errors=True)

    def test_cached_read_hit_and_miss(self):
        """Prove that the first read is a miss, and the second is a hit."""
        # 1. First read (Miss)
        res1 = cached_read(self.temp_file)
        self.assertFalse(res1.get("cached"), "First read should be a cache miss")
        self.assertIn("line 1", res1.get("content", ""))

        # 2. Second read (Hit)
        res2 = cached_read(self.temp_file)
        self.assertTrue(res2.get("cached"), "Second read should be a cache hit")
        self.assertEqual(res1.get("content"), res2.get("content"), "Cached data must match exactly")

    def test_cached_read_invalidation_on_modify(self):
        """Prove that modifying a file (mtime change) strictly invalidates the cache."""
        # 1. Cache the file
        res1 = cached_read(self.temp_file)
        self.assertFalse(res1.get("cached"))

        # Ensure mtime will definitely change (some OS have low mtime resolution)
        time.sleep(0.01)

        # 2. Modify the file
        with open(self.temp_file, "w") as f:
            f.write("MODIFIED LINE\n")

        # 3. Read again
        res2 = cached_read(self.temp_file)

        # Prove cache was busted!
        self.assertFalse(res2.get("cached"), "Cache MUST be invalidated after file modification")
        self.assertIn("MODIFIED LINE", res2.get("content", ""))
        self.assertNotEqual(res1.get("content"), res2.get("content"))

    def test_file_size_limit(self):
        """Prove that the default 5MB file size limit blocks large files (OOM protection)"""
        # We'll just write a 6MB file directly instead of mocking, as mocking os.stat breaks os.makedirs inside cache.py
        large_file = os.path.join(test_db_dir, "large_test.txt")
        try:
            with open(large_file, "wb") as f:
                f.seek((6 * 1024 * 1024) - 1)
                f.write(b"\0")

            res = cached_read(large_file)
            self.assertIn("error", res)
            self.assertIn("exceeds 5MB limit", res["error"])
        finally:
            if os.path.exists(large_file):
                os.remove(large_file)

    def test_file_size_limit_configurable(self):
        """Prove [cache].max_file_mb raises/lowers the cap deliberately.

        With max_file_mb=10 a 6MB file is readable (and no longer refused);
        with max_file_mb=1 a 2MB file is refused. Default stays 5MB.
        """
        from toolrecall.cache import config as cache_config
        from unittest.mock import patch as _patch

        real_get = cache_config.get

        def _scoped_get(value):
            # Override ONLY the max_file_mb key; pass everything else
            # through so ttl_policy and friends see the real config.
            def fake_get(*keys, **kwargs):
                if keys[:2] == ("cache", "max_file_mb"):
                    return value
                return real_get(*keys, **kwargs)

            return fake_get

        six_mb = os.path.join(test_db_dir, "six_mb.txt")
        two_mb = os.path.join(test_db_dir, "two_mb.txt")
        try:
            with open(six_mb, "wb") as f:
                f.seek((6 * 1024 * 1024) - 1)
                f.write(b"\0")
            with open(two_mb, "wb") as f:
                f.seek((2 * 1024 * 1024) - 1)
                f.write(b"\0")

            # Raise to 10MB: 6MB file now reads fine.
            with _patch.object(cache_config, "get", side_effect=_scoped_get(10)):
                res = cached_read(six_mb)
                self.assertNotIn("error", res)
                self.assertEqual(len(res.get("content", "")), 6 * 1024 * 1024)

            # Lower to 1MB: 2MB file now refused with the knob's value named.
            with _patch.object(cache_config, "get", side_effect=_scoped_get(1)):
                res = cached_read(two_mb)
                self.assertIn("error", res)
                self.assertIn("exceeds 1MB limit", res["error"])

            # Nonsense values (0, negative, absurd) fall back to 5MB.
            for bad in (0, -3, 99999):
                with _patch.object(cache_config, "get", side_effect=_scoped_get(bad)):
                    res = cached_read(two_mb)
                    self.assertNotIn("error", res)
        finally:
            for p in (six_mb, two_mb):
                if os.path.exists(p):
                    os.remove(p)

    def test_single_counting_file_cache(self):
        """Prove tokens_read_from_disk is counted exactly once per unique file (disk-read), not on cache hits."""
        from toolrecall.cache import get_stats, reset_stats, _estimate_tokens

        reset_stats()

        # Create fresh file never seen before
        fd2, f2 = tempfile.mkstemp(text=True)
        with os.fdopen(fd2, "w") as f:
            f.write("Hello World! " * 50)
        expected_tokens = _estimate_tokens("Hello World! " * 50)

        # 1st read: disk miss → counted once
        r1 = cached_read(f2)
        self.assertFalse(r1.get("cached"))

        stats_after_first = get_stats().get("file_cache", {})
        tokens_read_from_disk = stats_after_first.get("tokens_read_from_disk", 0)
        self.assertEqual(
            tokens_read_from_disk,
            expected_tokens,
            f"First disk-read should count tokens exactly once (got {tokens_read_from_disk}, expected {expected_tokens})",
        )

        # 2nd read: in-memory hit → NO new tokens
        r2 = cached_read(f2)
        self.assertTrue(r2.get("cached"))

        # 3rd: force SQLite hit by clearing in-memory
        from toolrecall.cache import _file_cache

        _file_cache.remove(f2)
        r3 = cached_read(f2)
        self.assertTrue(r3.get("cached"))

        stats_after_three = get_stats().get("file_cache", {})
        tokens_after_three = stats_after_three.get("tokens_read_from_disk", 0)
        self.assertEqual(
            tokens_after_three,
            expected_tokens,
            f"Tokens should NOT increase on cache hits (got {tokens_after_three}, expected {expected_tokens})",
        )
        self.assertEqual(stats_after_three["hits"], 2, "Should have 2 hits (2nd read + 3rd read)")

        os.unlink(f2)

    def test_reset_stats_preserves_entries(self):
        """Prove reset_stats clears counters but keeps cache entries intact."""
        from toolrecall.cache import get_stats, reset_stats

        # Populate via a miss
        fd3, f3 = tempfile.mkstemp(text=True)
        with os.fdopen(fd3, "w") as f:
            f.write("test data")
        cached_read(f3)

        entries_before = get_stats().get("file_cache_entries", 0)
        self.assertGreater(entries_before, 0, "Should have entries")

        reset_stats()

        stats = get_stats()
        # Counters reset to zero — the row stays and reads 0, because the reset
        # moves its totals into cache_stats_banked instead of deleting them
        # (2026-10-22). Absence from this dict used to mean "deleted"; a zeroed
        # window next to a readable lifetime_* is the honest version, and it is
        # what makes the total visible instead of merely stored.
        fc = stats.get("file_cache", {})
        self.assertEqual(fc.get("hits"), 0, "live window counters must be reset")
        self.assertEqual(fc.get("misses"), 0, "live window counters must be reset")
        self.assertGreaterEqual(
            fc.get("lifetime_misses", 0), 1, "the reset must keep the total readable"
        )
        # Entries preserved
        entries_after = stats.get("file_cache_entries", 0)
        self.assertEqual(entries_after, entries_before, "Cache entries should survive reset_stats")

        os.unlink(f3)

    def test_reset_stats_logs_caller_stack(self):
        """reset_stats audit line must name the caller (file:line:func chain).

        The 2026-09-10 wipe storm (17 production resets in 5h) was attributable
        only by timestamp correlation because the audit line carried no caller.
        This guards the caller-stack addition so forensics never regress.
        """
        from toolrecall.cache import reset_stats

        with self.assertLogs("toolrecall.cache", level="WARNING") as captured:
            reset_stats()
        # Anchor on the stable prefix, not on "wiped": the reset now MOVES the
        # counters into cache_stats_banked (2026-10-22), so claiming a wipe
        # would make the audit line untrue.
        audit_lines = [ln for ln in captured.output if "reset_stats:" in ln]
        self.assertTrue(audit_lines, f"no audit line in: {captured.output}")
        self.assertIn(
            "cache_stats_banked",
            audit_lines[0],
            f"audit line must say where the counters went: {audit_lines[0]}",
        )
        self.assertIn(
            "caller stack:",
            audit_lines[0],
            f"audit line missing caller stack: {audit_lines[0]}",
        )
        self.assertIn(
            "test_reset_stats_logs_caller_stack",
            audit_lines[0],
            f"caller stack must name this test function: {audit_lines[0]}",
        )

    def test_reset_stats_all_layers(self):
        """Prove reset_stats clears all 6 cache layer counters."""
        from toolrecall.cache import get_stats, reset_stats

        reset_stats()
        stats = get_stats()
        for layer in [
            "file_cache",
            "skill_cache",
            "terminal_cache",
            "script_cache",
            "code_cache",
            "mcp_cache",
        ]:
            self.assertNotIn(
                layer, stats, f"'{layer}' should be absent after reset_stats (category deleted)"
            )


if __name__ == "__main__":
    unittest.main()
