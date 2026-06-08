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
        if os.path.exists(test_db_path):
            os.remove(test_db_path)
        _init()
        
        # Create a temporary file for reading
        self.fd, self.temp_file = tempfile.mkstemp(text=True)
        with os.fdopen(self.fd, 'w') as f:
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
        with open(self.temp_file, 'w') as f:
            f.write("MODIFIED LINE\n")
            
        # 3. Read again
        res2 = cached_read(self.temp_file)
        
        # Prove cache was busted!
        self.assertFalse(res2.get("cached"), "Cache MUST be invalidated after file modification")
        self.assertIn("MODIFIED LINE", res2.get("content", ""))
        self.assertNotEqual(res1.get("content"), res2.get("content"))

    def test_file_size_limit(self):
        """Prove that the 5MB file size limit blocks large files (OOM protection)"""
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

    def test_single_counting_file_cache(self):
        """Prove tokens_intercepted is counted exactly once per unique file (disk-read), not on cache hits."""
        from toolrecall.cache import get_stats, reset_stats, _estimate_tokens

        reset_stats()

        # Create fresh file never seen before
        fd2, f2 = tempfile.mkstemp(text=True)
        with os.fdopen(fd2, 'w') as f:
            f.write("Hello World! " * 50)
        expected_tokens = _estimate_tokens("Hello World! " * 50)

        # 1st read: disk miss → counted once
        r1 = cached_read(f2)
        self.assertFalse(r1.get("cached"))

        stats_after_first = get_stats().get("file_cache", {})
        self.assertEqual(
            stats_after_first["tokens_intercepted"],
            expected_tokens,
            "First disk-read should count tokens exactly once"
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
        self.assertEqual(
            stats_after_three["tokens_intercepted"],
            expected_tokens,
            "Tokens should NOT increase on cache hits (in-memory or SQLite)"
        )
        self.assertEqual(stats_after_three["hits"], 2, "Should have 2 hits (2nd read + 3rd read)")

        os.unlink(f2)

    def test_reset_stats_preserves_entries(self):
        """Prove reset_stats clears counters but keeps cache entries intact."""
        from toolrecall.cache import get_stats, reset_stats

        # Populate via a miss
        fd3, f3 = tempfile.mkstemp(text=True)
        with os.fdopen(fd3, 'w') as f:
            f.write("test data")
        cached_read(f3)

        entries_before = get_stats().get("file_cache_entries", 0)
        self.assertGreater(entries_before, 0, "Should have entries")

        reset_stats()

        stats = get_stats()
        # Counters gone
        self.assertNotIn("file_cache", stats, "file_cache stats should be reset")
        # Entries preserved
        entries_after = stats.get("file_cache_entries", 0)
        self.assertEqual(entries_after, entries_before,
                         "Cache entries should survive reset_stats")

        os.unlink(f3)

    def test_reset_stats_all_layers(self):
        """Prove reset_stats clears all 6 cache layer counters."""
        from toolrecall.cache import get_stats, reset_stats

        reset_stats()
        stats = get_stats()
        for layer in ["file_cache", "skill_cache", "terminal_cache", "script_cache", "code_cache", "mcp_cache"]:
            self.assertNotIn(layer, stats,
                             f"'{layer}' should be absent after reset_stats (category deleted)")


if __name__ == "__main__":
    unittest.main()
