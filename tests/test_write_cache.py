"""Tests for cached_write and cached_patch — write/patch dedup cache.

These are stateless idempotency checks (not persisted to SQLite).
They compare content to disk and skip redundant operations.
"""
import os
import sys
import unittest
import tempfile
import shutil

# Force a clean, isolated test database path before loading toolrecall
test_db_dir = tempfile.mkdtemp()
test_db_path = os.path.join(test_db_dir, "test_cache.db")
os.environ["TOOLRECALL_CACHE_DB"] = test_db_path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from toolrecall.cache import cached_write, cached_patch, _init


class TestCachedWrite(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        os.environ["TOOLRECALL_CACHE_DB"] = test_db_path
        _init()

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_write_new_file(self):
        """Writing to a non-existent file should always work (no skip)."""
        path = os.path.join(self.tmpdir, "new.txt")
        result = cached_write(path, "hello world")
        self.assertFalse(result.get("unchanged", False))
        self.assertEqual(result.get("path"), path)
        self.assertTrue(os.path.exists(path))
        with open(path) as f:
            self.assertEqual(f.read(), "hello world")

    def test_write_identical_content(self):
        """Writing identical content should return unchanged=True."""
        path = os.path.join(self.tmpdir, "same.txt")
        with open(path, "w") as f:
            f.write("hello world")

        result = cached_write(path, "hello world")
        self.assertTrue(result.get("unchanged", False))
        self.assertEqual(result.get("path"), path)

    def test_write_different_content(self):
        """Writing different content to an existing file should overwrite."""
        path = os.path.join(self.tmpdir, "diff.txt")
        with open(path, "w") as f:
            f.write("old content")

        result = cached_write(path, "new content")
        self.assertFalse(result.get("unchanged", False))
        self.assertFalse(result.get("cached", True))
        with open(path) as f:
            self.assertEqual(f.read(), "new content")

    def test_write_nested_directory_creates_parents(self):
        """Writing to a path with non-existent parent dirs should create them."""
        path = os.path.join(self.tmpdir, "nested", "deep", "file.txt")
        result = cached_write(path, "nested content")
        self.assertFalse(result.get("unchanged", False))
        self.assertTrue(os.path.exists(path))
        with open(path) as f:
            self.assertEqual(f.read(), "nested content")

    def test_write_large_file_bypasses_dedup(self):
        """Files over 5MB should skip dedup and always write."""
        path = os.path.join(self.tmpdir, "large.bin")
        # Create a 5MB+ file
        large_content = "x" * (5 * 1024 * 1024 + 1)
        with open(path, "w") as f:
            f.write(large_content)

        # Writing the same large content — should bypass dedup and write
        result = cached_write(path, large_content)
        # Should not return unchanged because it large-file paths always writes
        self.assertFalse(result.get("unchanged", False))
        self.assertFalse(result.get("cached", True))

    def test_write_empty_content(self):
        """Writing empty string to an existing file should still check and skip."""
        path = os.path.join(self.tmpdir, "empty.txt")
        with open(path, "w") as f:
            f.write("")

        result = cached_write(path, "")
        self.assertTrue(result.get("unchanged", False))

    def test_write_to_same_path_twice_first_miss_second_hit(self):
        """First write with new content = miss, second write with same = hit."""
        path = os.path.join(self.tmpdir, "twice.txt")
        r1 = cached_write(path, "version one")
        self.assertFalse(r1.get("unchanged", False))

        r2 = cached_write(path, "version one")
        self.assertTrue(r2.get("unchanged", False))


class TestCachedPatch(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        os.environ["TOOLRECALL_CACHE_DB"] = test_db_path
        _init()

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _write(self, path, content):
        """Helper to write a test file."""
        full = os.path.join(self.tmpdir, path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w") as f:
            f.write(content)
        return full

    def test_patch_already_applied(self):
        """If new_string already replaces old_string, return already_applied."""
        path = self._write("test.txt", "hello brave new world")
        result = cached_patch(path, "old", "brave")
        self.assertTrue(result.get("unchanged", False))
        self.assertEqual(result.get("reason"), "already_applied")

    def test_patch_already_applied_early(self):
        """When old_string not present but new_string is, still no-op."""
        path = self._write("test.txt", "hello already there")
        result = cached_patch(path, "nothing", "already")
        self.assertTrue(result.get("unchanged", False))
        self.assertEqual(result.get("reason"), "already_applied")

    def test_patch_not_found(self):
        """If old_string is not present and new_string isn't either, return not_found."""
        path = self._write("test.txt", "hello world")
        result = cached_patch(path, "nope", "neither")
        self.assertTrue(result.get("unchanged", False))
        self.assertEqual(result.get("reason"), "not_found")

    def test_patch_applies(self):
        """Actual find-and-replace should apply the patch."""
        path = self._write("test.txt", "hello old world")
        result = cached_patch(path, "old", "brave new")
        self.assertFalse(result.get("unchanged", False))
        self.assertFalse(result.get("cached", True))
        with open(path) as f:
            self.assertEqual(f.read(), "hello brave new world")

    def test_patch_file_not_found(self):
        """Patch on non-existent file should return error."""
        path = os.path.join(self.tmpdir, "nonexistent.txt")
        result = cached_patch(path, "a", "b")
        self.assertIn("error", result)

    def test_patch_then_write_then_patch_again(self):
        """Patch applies (miss), then write same content (hit for write), then patch already applied (hit for patch)."""
        path = self._write("cycle.txt", "start")
        # Patch it
        r1 = cached_patch(path, "start", "updated")
        self.assertFalse(r1.get("unchanged", False))
        # Re-patch same — should be already_applied
        r2 = cached_patch(path, "start", "updated")
        self.assertTrue(r2.get("unchanged", False))
        self.assertEqual(r2.get("reason"), "already_applied")

    def test_patch_multiple_occurrences_first_only(self):
        """cached_patch only replaces the first occurrence (like Hermes' patch tool)."""
        path = self._write("multi.txt", "a a a")
        result = cached_patch(path, "a", "b")
        self.assertFalse(result.get("unchanged", False))
        self.assertEqual(result.get("changes"), 3)
        with open(path) as f:
            self.assertEqual(f.read(), "b a a")


class TestCachedWritePatchIntegration(unittest.TestCase):
    """Tests where cached_write and cached_patch interact."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        os.environ["TOOLRECALL_CACHE_DB"] = test_db_path
        _init()

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_write_then_patch_same_file(self):
        """Write a file, then patch it — both operations should chain cleanly."""
        path = os.path.join(self.tmpdir, "chain.txt")

        # Write
        r1 = cached_write(path, "hello world")
        self.assertFalse(r1.get("unchanged", False))

        # Patch: identical write skipped
        r2 = cached_write(path, "hello world")
        self.assertTrue(r2.get("unchanged", False))

        # Patch the content
        r3 = cached_patch(path, "world", "there")
        self.assertFalse(r3.get("unchanged", False))

        # Patch again — already applied
        r4 = cached_patch(path, "world", "there")
        self.assertTrue(r4.get("unchanged", False))

        # Verify final content
        with open(path) as f:
            self.assertEqual(f.read(), "hello there")

    def test_triple_noop(self):
        """Write identical → patch already applied → write identical again = all hits."""
        path = os.path.join(self.tmpdir, "triple.txt")
        cached_write(path, "content")
        self.assertTrue(cached_write(path, "content").get("unchanged"))
        self.assertTrue(cached_patch(path, "missing", "content").get("unchanged"))
        self.assertTrue(cached_write(path, "content").get("unchanged"))


if __name__ == "__main__":
    unittest.main()
