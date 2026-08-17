"""Regression tests: shim/cache correctness under terminal & bash-style usage.

Two bugs were fixed so the shim behaves like a drop-in for ``open()`` and
never hands the caller a stale or fake file object:

  1. **Real file object on cache hit.** Previously the shim served cache hits
     as ``io.StringIO``. StringIO has no ``.fileno()``, ``.buffer`` or
     ``.name``, so terminal/bash tooling that pipes a cached file into a
     subprocess (``subprocess.run(cmd, stdin=open(f))``), calls ``os.fstat``,
     ``mmap`` or ``select`` crashed with ``UnsupportedOperation: fileno``.
     The shim now materializes the cached bytes in a temporary file and
     returns a real handle.

  2. **Size-aware invalidation.** The cache hit path compared only ``mtime``,
     so a same-mtime rewrite (fast writes, ``os.utime`` pinning, coarse-mtime
     filesystems) served stale bytes — the "hallucination / stale info"
     failure mode. Hits now require both ``mtime`` AND ``size`` to match.
"""

import io
import os
import subprocess
import tempfile
import unittest

import toolrecall.shim as shim_mod
import builtins


def _force_hit(content="line\nline2\n"):
    """Make _get_tr() return a stub cached_read that always returns a HIT."""
    shim_mod._SKIP_PREFIXES = []
    shim_mod._TR = {"read": lambda p, **kw: {"cached": True, "content": content}}


class TestCacheHitIsARealFile(unittest.TestCase):
    """F1: cache hits behave like real file objects (fileno/buffer/name)."""

    def setUp(self):
        self._tr = shim_mod._TR
        self._sk = shim_mod._SKIP_PREFIXES
        self._orig_open = builtins.open
        shim_mod._thread_local.active = False

    def tearDown(self):
        shim_mod._TR = self._tr
        shim_mod._SKIP_PREFIXES = self._sk
        shim_mod._thread_local.active = False
        builtins.open = self._orig_open

    def test_hit_returns_real_file_not_stringio(self):
        _force_hit()
        fh = shim_mod._shim_open("/some/file", "r")
        self.assertNotIsInstance(fh, io.StringIO)
        self.assertEqual(fh.read(), "line\nline2\n")
        fh.close()

    def test_hit_exposes_fileno(self):
        _force_hit()
        fh = shim_mod._shim_open("/some/file", "r")
        try:
            fd = fh.fileno()
            self.assertGreaterEqual(fd, 0)
            # os.fstat works on the returned descriptor
            os.fstat(fd)
        finally:
            fh.close()

    def test_hit_exposes_buffer_and_name(self):
        _force_hit()
        fh = shim_mod._shim_open("/some/file", "r")
        try:
            self.assertTrue(hasattr(fh, "buffer"))
            self.assertIsNotNone(fh.name)
            self.assertTrue(fh.name.endswith(".tmp"))
        finally:
            fh.close()

    def test_hit_readable_and_shares_fd(self):
        _force_hit("12345")
        fh = shim_mod._shim_open("/some/file", "r")
        try:
            self.assertEqual(fh.read(), "12345")
            fh.seek(3)
            self.assertEqual(fh.read(), "45")
        finally:
            fh.close()

    def test_subprocess_stdin_uses_shim_file(self):
        """Piping a shim-cached file into a subprocess must work (needs fileno)."""
        _force_hit("KEY=value\n")
        fh = shim_mod._shim_open("/some/file", "r")
        try:
            proc = subprocess.run(["cat"], stdin=fh, capture_output=True, text=True)
            self.assertEqual(proc.stdout, "KEY=value\n")
        finally:
            fh.close()

    def test_temp_file_cleaned_up(self):
        _force_hit("data")
        fh = shim_mod._shim_open("/some/file", "r")
        name = fh.name
        fh.close()
        # once the handle closes, the temp file must be gone (POSIX unlink)
        try:
            os.stat(name)
        except FileNotFoundError:
            pass  # expected
        else:
            self.fail(f"temporary file was not removed: {name}")


class TestSizeAwareInvalidation(unittest.TestCase):
    """F2: cache must not serve stale bytes when the file changed."""

    def setUp(self):
        self._tr = shim_mod._TR
        self._sk = shim_mod._SKIP_PREFIXES
        shim_mod._thread_local.active = False

    def tearDown(self):
        shim_mod._TR = self._tr
        shim_mod._SKIP_PREFIXES = self._sk
        shim_mod._thread_local.active = False

    def test_same_mtime_size_change_invalidates(self):
        """A rewrite at the same mtime but a DIFFERENT size must be a miss
        (size-aware invalidation), so the caller gets the fresh on-disk bytes."""
        from toolrecall.cache import cached_read

        tmp = tempfile.mktemp(suffix=".txt")
        with builtins.open(tmp, "w") as f:
            f.write("AAAA")  # size 4
        os.utime(tmp, (1e9, 1e9))
        r = cached_read(tmp, source="shim")
        assert r["content"] == "AAAA"

        # Rewrite different-size content, pin the SAME mtime.
        with builtins.open(tmp, "w") as f:
            f.write("BBBBBBBB")  # size 8 != 4
        os.utime(tmp, (1e9, 1e9))  # identical mtime, DIFFERENT size

        r2 = cached_read(tmp, source="shim")
        self.assertEqual(r2["content"], "BBBBBBBB", "size changed but cache served stale bytes")
        self.assertFalse(r2.get("cached"))
        try:
            os.unlink(tmp)
        except OSError:
            pass

    def test_size_change_invalidates_despite_same_mtime(self):
        """A rewrite at the same mtime but a DIFFERENT size must be a miss."""
        from toolrecall.cache import cached_read

        tmp = tempfile.mktemp(suffix=".txt")
        with builtins.open(tmp, "w") as f:
            f.write("FIRST")
        os.utime(tmp, (1e9, 1e9))
        cached_read(tmp, source="shim")  # warm

        with builtins.open(tmp, "w") as f:
            f.write("SECOND-LONGER")  # different size
        os.utime(tmp, (1e9, 1e9))  # same mtime, different size

        r = cached_read(tmp, source="shim")
        self.assertEqual(r["content"], "SECOND-LONGER")
        self.assertFalse(r.get("cached"))
        try:
            os.unlink(tmp)
        except OSError:
            pass


if __name__ == "__main__":
    unittest.main()
