"""E2E: Daemon lifecycle — start, ping, stop, restart, wrong socket."""

import os
import sys
import unittest
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tests.e2e_helpers import E2EDaemon


class TestDaemonLifecycle(unittest.TestCase):
    """Start a real daemon, verify it responds, stop it, verify it's gone."""

    def test_daemon_starts_and_pings(self):
        """Daemon accepts ping after startup."""
        with E2EDaemon() as d:
            result = d.client.send({"cmd": "ping"})
            self.assertIn("pong", result)
            self.assertTrue(result["pong"])

    def test_daemon_stops_cleanly(self):
        """After stop, socket file is removed and daemon process is gone."""
        d = E2EDaemon()
        d.start()
        self.assertTrue(d.running)
        self.assertTrue(os.path.exists(d.socket_path))
        d.stop()
        self.assertFalse(d.running)
        self.assertFalse(os.path.exists(d.socket_path))

    def test_daemon_restart(self):
        """Daemon can be stopped and restarted on a new socket."""
        d = E2EDaemon()
        with d:
            r1 = d.client.send({"cmd": "ping"})
            self.assertTrue(r1.get("pong"))
        # Recreate with NEW socket path
        d2 = E2EDaemon()
        with d2:
            r2 = d2.client.send({"cmd": "ping"})
            self.assertTrue(r2.get("pong"))
        self.assertNotEqual(d.socket_path, d2.socket_path)

    def test_daemon_rejects_on_wrong_socket(self):
        """Connecting to a non-existent socket returns error, not crash."""
        bogus = os.path.join(tempfile.mkdtemp(), "nonexistent.sock")
        # Import here to avoid ImportError on test collection if transport is broken
        from toolrecall.transport import TransportClient
        client = TransportClient(path=bogus)
        result = client.send({"cmd": "ping"})
        self.assertIn("error", result)