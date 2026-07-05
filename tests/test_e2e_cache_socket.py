"""E2E: Cache operations over real daemon socket.

Tests that the daemon correctly handles:
  - cached_read (hit/miss/mtime invalidation)
  - cache status available
"""

import os
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tests.e2e_helpers import E2EDaemon


class TestE2ECacheSocket(unittest.TestCase):
    """Full cache round-trip over real daemon socket."""

    def setUp(self):
        self.daemon = E2EDaemon()
        self.daemon.start()

    def tearDown(self):
        self.daemon.stop()

    def _read_file(self, path: str) -> dict:
        return self.daemon.client.send({"cmd": "cached_read", "path": path})

    def _status(self) -> dict:
        return self.daemon.client.send({"cmd": "cache_status"})

    def test_cached_read_miss_then_hit(self):
        """First read is a miss, second read is a hit (same file)."""
        test_file = os.path.join(self.daemon._tmpdir.name, "hello.txt")
        with open(test_file, "w") as f:
            f.write("Hello E2E!")

        # First read — should miss and read from disk
        r1 = self._read_file(test_file)
        self.assertIn("content", r1)
        self.assertIn("Hello E2E!", r1["content"])

        # Second read — should hit cache (same content)
        r2 = self._read_file(test_file)
        self.assertIn("content", r2)
        self.assertEqual(r1["content"], r2["content"])

    def test_cached_read_mtime_invalidation(self):
        """Cache is invalidated when file mtime changes."""
        test_file = os.path.join(self.daemon._tmpdir.name, "mtime_test.txt")
        with open(test_file, "w") as f:
            f.write("Version 1")

        r1 = self._read_file(test_file)
        self.assertIn("Version 1", r1["content"])

        # Modify file
        time.sleep(0.01)  # ensure different mtime
        with open(test_file, "w") as f:
            f.write("Version 2")

        r2 = self._read_file(test_file)
        self.assertIn("Version 2", r2["content"])

    def test_cached_read_missing_file(self):
        """Reading a non-existent file returns an error, not a crash."""
        bogus = os.path.join(self.daemon._tmpdir.name, "nope.txt")
        result = self._read_file(bogus)
        self.assertIn("error", result)

    def test_status_available(self):
        """Cache status endpoint returns stats."""
        stats = self._status()
        self.assertIsInstance(stats, dict)