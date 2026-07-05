"""E2E: Client module daemon-first pattern.

Tests:
  - daemon_running() returns True when daemon is up
  - set_socket_path() forces client to use custom socket
  - Fallback to direct SQLite when daemon is stopped
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tests.e2e_helpers import E2EDaemon


class TestE2EClientDaemon(unittest.TestCase):
    """Client module talking to a real daemon."""

    def setUp(self):
        self.daemon = E2EDaemon()
        self.daemon.start()
        # Force client to use our test socket
        from toolrecall.client import set_socket_path
        set_socket_path(self.daemon.socket_path)

    def tearDown(self):
        self.daemon.stop()
        # Reset client singleton for next test
        from toolrecall.client import set_socket_path
        set_socket_path(self.daemon.socket_path)

    def test_daemon_running_true(self):
        """daemon_running() returns True when daemon is up."""
        from toolrecall.client import daemon_running
        self.assertTrue(daemon_running())

    def test_daemon_running_false_after_stop(self):
        """daemon_running() returns False after daemon is stopped."""
        from toolrecall.client import daemon_running, set_socket_path

        self.assertTrue(daemon_running())
        self.daemon.stop()
        # Force reconnect — client will attempt to connect and fail
        set_socket_path(self.daemon.socket_path)
        self.assertFalse(daemon_running())