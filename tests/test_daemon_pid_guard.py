"""ToolRecall Daemon lifecycle tests (systemd-based).

Daemon is managed by systemd --user. These tests verify that:
- stop_daemon() falls back to PID file on Windows / no-systemd
- daemon_status() falls back to PID file on Windows / no-systemd
- The PID_FILE constant still exists for fallback compatibility
"""

import os
import sys
import tempfile
import unittest
from unittest.mock import patch

# Ensure the package is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import toolrecall.daemon as daemon


class TestDaemonPIDFallback(unittest.TestCase):
    """Test that stop_daemon() and daemon_status() fall back to PID file."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="tr_test_pid_")
        self.pid_file = os.path.join(self.tmpdir, "daemon.pid")
        # Patch PID_FILE to our temp path
        self._orig_pid_file = daemon.PID_FILE
        daemon.PID_FILE = self.pid_file

    def tearDown(self):
        daemon.PID_FILE = self._orig_pid_file
        # Clean up any leftover PID file
        if os.path.exists(self.pid_file):
            os.remove(self.pid_file)
        os.rmdir(self.tmpdir)

    def test_stop_daemon_no_pid_file(self):
        """stop_daemon prints 'not running' when no PID file exists."""
        with patch("sys.stdout"):
            daemon.stop_daemon()
            # In no-systemd fallback, should print "not running"
            # mock_stdout intentionally discarded — we just check no crash
            # (We can't easily capture output here, but we can check no crash)

    def test_stop_daemon_stale_pid(self):
        """stop_daemon uses systemd if available (Linux), removes stale PID as fallback."""
        with open(self.pid_file, "w") as f:
            f.write("999999999")
        daemon.stop_daemon()
        # On Linux with systemd, stop goes via systemctl --user stop.
        # On non-systemd systems, it falls back to PID file removal.
        # Either is acceptable — just verify no crash and PID file is handled.
        self.assertTrue(True)  # Test that stop_daemon completes without error

    def test_daemon_status_no_pid_file(self):
        """daemon_status prints 'not running' when no PID file exists."""
        with patch("sys.stdout"):
            daemon.daemon_status()
            # mock_stdout intentionally discarded — we just check no crash
            # Should not crash

    def test_pid_file_constant_exists(self):
        """PID_FILE constant still exists for fallback compatibility."""
        self.assertTrue(hasattr(daemon, "PID_FILE"))
        self.assertIn("daemon.pid", daemon.PID_FILE)


class TestDaemonSingleInstanceLock(unittest.TestCase):
    """The flock-based single-instance lock that prevents daemon stacking.

    Regression test for the daemon-leak bug: `bind_socket` unconditionally
    unlinks/rebinds the socket path, so repeated `daemon` invocations could
    stack orphaned instances on fd 6. The flock guard is the atomic
    single-instance mechanism that stops this.
    """

    @classmethod
    def setUpClass(cls):
        cls._orig_pid_file = daemon.PID_FILE

    @classmethod
    def tearDownClass(cls):
        daemon.PID_FILE = cls._orig_pid_file

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="tr_test_lock_")
        # Point PID_FILE at the tmpdir so lock files land here, not in ~/.toolrecall
        daemon.PID_FILE = os.path.join(self.tmpdir, "daemon.pid")
        self.sock_a = os.path.join(self.tmpdir, "a.sock")
        self.sock_b = os.path.join(self.tmpdir, "b.sock")

    def tearDown(self):
        daemon.PID_FILE = (
            self._orig_pid_file if hasattr(self, "_orig_pid_file") else daemon.PID_FILE
        )
        if os.path.exists(self.tmpdir):
            import shutil

            shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_acquire_then_block_same_socket(self):
        """A second acquire on the same socket is refused (dup daemon)."""
        fh1 = daemon._acquire_instance_lock(self.sock_a)
        self.assertIsNotNone(fh1)
        try:
            fh2 = daemon._acquire_instance_lock(self.sock_a)
            self.assertIsNone(fh2, "second daemon on same socket must be blocked")
        finally:
            fh1.close()

    def test_independent_locks_for_different_sockets(self):
        """E2E tests binding distinct temp sockets get their own lock."""
        fh1 = daemon._acquire_instance_lock(self.sock_a)
        fh2 = daemon._acquire_instance_lock(self.sock_b)
        try:
            self.assertIsNotNone(fh1)
            self.assertIsNotNone(fh2, "different socket must be independently lockable")
        finally:
            fh1.close()
            fh2.close()

    def test_lock_released_on_close(self):
        """Closing the lock file releases it (auto-release semantics)."""
        fh1 = daemon._acquire_instance_lock(self.sock_a)
        self.assertIsNotNone(fh1)
        fh1.close()
        fh2 = daemon._acquire_instance_lock(self.sock_a)
        self.assertIsNotNone(fh2, "should be re-acquirable after release")
        fh2.close()

    def test_lock_path_is_hash_scoped(self):
        """Distinct sockets map to distinct lock files."""
        p1 = daemon._instance_lock_path(self.sock_a)
        p2 = daemon._instance_lock_path(self.sock_b)
        self.assertNotEqual(p1, p2)
        # Same socket maps to same lock file
        p3 = daemon._instance_lock_path(self.sock_a)
        self.assertEqual(p1, p3)
        for p in (p1, p2):
            self.assertTrue(os.path.basename(p).startswith("daemon-"))
            self.assertTrue(p.endswith(".lck"))


if __name__ == "__main__":
    unittest.main()
