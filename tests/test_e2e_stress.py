"""E2E: Stress test — concurrent requests, rapid restart."""

import os
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tests.e2e_helpers import E2EDaemon


class TestE2EStress(unittest.TestCase):
    """Daemon under load."""

    def test_concurrent_requests(self):
        """10 concurrent ping requests all succeed."""
        with E2EDaemon() as d:
            errors = []
            lock = threading.Lock()

            def ping_once():
                try:
                    r = d.client.send({"cmd": "ping"})
                    assert r.get("pong") is True, f"Unexpected: {r}"
                except Exception as e:
                    with lock:
                        errors.append(str(e))

            threads = [threading.Thread(target=ping_once) for _ in range(10)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=5)

            self.assertEqual([], errors, f"{len(errors)} concurrent requests failed")

    def test_rapid_restart(self):
        """Daemon can be started and stopped 5 times in quick succession."""
        for i in range(5):
            d = E2EDaemon()
            d.start()
            self.assertTrue(d.running, f"Failed on iteration {i+1}")
            d.stop()
            self.assertFalse(d.running, f"Failed to stop on iteration {i+1}")