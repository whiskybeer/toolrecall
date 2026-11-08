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

from toolrecall.cache import cached_read, cached_write, cached_patch, _init


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

    def test_write_detects_change_against_crlf_file(self):
        """LF content over a CRLF file is a real change, not 'unchanged'.

        The old comparison read the file with universal-newline translation, so
        a CRLF file looked identical to the agent's LF string and the write was
        silently skipped — the file kept endings the agent never wrote.
        """
        path = os.path.join(self.tmpdir, "crlf_write.txt")
        with open(path, "wb") as f:
            f.write(b"alpha\r\nbeta\r\n")
        result = cached_write(path, "alpha\nbeta\n")
        self.assertFalse(result.get("unchanged", False), result)
        with open(path, "rb") as f:
            self.assertEqual(f.read(), b"alpha\nbeta\n")


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

    def test_patch_deletion_applies(self):
        """A deletion patch (new_string="") must delete, not report already_applied.

        ``"" in content`` is always true, so a substring-containment check reports a
        deletion as already applied and silently leaves the file untouched.
        """
        path = self._write("del.txt", "keep REMOVE tail")
        result = cached_patch(path, "REMOVE ", "")
        self.assertFalse(result.get("unchanged", False))
        with open(path) as f:
            self.assertEqual(f.read(), "keep tail")

    def test_patch_applies_when_new_text_occurs_earlier(self):
        """A pre-existing occurrence of new_string elsewhere must not suppress the patch.

        Position is not evidence of prior application: if new_string already appears
        before old_string (boilerplate, a sibling function), the patch was NOT applied.
        """
        path = self._write("earlier.txt", "return x\npass\nreturn x + 1\n")
        result = cached_patch(path, "return x + 1", "return x")
        self.assertFalse(result.get("unchanged", False))
        with open(path) as f:
            self.assertEqual(f.read(), "return x\npass\nreturn x\n")

    def test_patch_reapply_is_still_unchanged(self):
        """Re-applying an applied patch stays a no-op (idempotence guard)."""
        path = self._write("idem.txt", "start")
        cached_patch(path, "start", "done")
        r2 = cached_patch(path, "start", "done")
        self.assertTrue(r2.get("unchanged", False))
        self.assertEqual(r2.get("reason"), "already_applied")

    def test_patch_preserves_crlf_endings(self):
        """Patching one line must not rewrite the file with LF endings.

        The old read used universal-newline translation (no newline=""), so
        every \\r\\n became \\n in memory and the whole file was written back
        with LF — a one-line patch silently converted the file.
        """
        path = os.path.join(self.tmpdir, "crlf_patch.txt")
        with open(path, "wb") as f:
            f.write(b"alpha\r\nBETA\r\n")
        result = cached_patch(path, "BETA", "GAMMA")
        self.assertFalse(result.get("unchanged", False), result)
        with open(path, "rb") as f:
            self.assertEqual(f.read(), b"alpha\r\nGAMMA\r\n")

    def test_patch_refuses_non_utf8_instead_of_corrupting(self):
        """A non-UTF-8 file is refused, never silently rewritten lossy.

        errors="ignore" dropped undecodable bytes and the UTF-8 rewrite then
        deleted them from disk (latin-1 'café' came back as 'caf').
        """
        path = os.path.join(self.tmpdir, "latin1.txt")
        raw = "café crème".encode("latin-1")
        with open(path, "wb") as f:
            f.write(raw)
        result = cached_patch(path, "crème", "thé")
        self.assertIn("error", result, result)
        with open(path, "rb") as f:
            self.assertEqual(f.read(), raw, "file must be untouched when refused")


class TestPathIdentity(unittest.TestCase):
    """A relative path must never be cached under its bare name.

    Cache keys are derived from the path string and the daemon opens the path
    against its OWN cwd, so two callers in different directories shared one key
    and one file (reading NOTES.md in project B returned project A's content).
    """

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        os.environ["TOOLRECALL_CACHE_DB"] = test_db_path
        _init()

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_cached_read_rejects_relative_path(self):
        result = cached_read("notes.md")
        self.assertIn("error", result, result)

    def test_cached_write_rejects_relative_path(self):
        result = cached_write("notes.md", "content")
        self.assertIn("error", result, result)

    def test_cached_patch_rejects_relative_path(self):
        result = cached_patch("notes.md", "a", "b")
        self.assertIn("error", result, result)


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
