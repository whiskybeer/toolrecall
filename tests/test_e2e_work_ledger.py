"""E2E: work-unit ledger over a real daemon socket.

The unit tests cover the ledger functions; this covers the wire: the daemon
commands, the response envelope, and the `work_cache` counter that `top` and
the healthcheck read.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tests.e2e_helpers import E2EDaemon


class TestE2EWorkLedger(unittest.TestCase):
    """Full store → reuse → counters round-trip over the daemon socket."""

    def setUp(self):
        self.daemon = E2EDaemon()
        self.daemon.start()

    def tearDown(self):
        self.daemon.stop()

    def _send(self, cmd: str, **kw) -> dict:
        return self.daemon.client.send({"cmd": cmd, **kw})

    def test_store_lookup_and_counters_over_socket(self):
        stored = self._send(
            "work_store", key="unit-a", metrics={"combined_score": 0.25}, eval_time=0.5
        )
        self.assertTrue(stored.get("result", {}).get("stored"), stored)

        hit = self._send("work_lookup", key="unit-a")
        self.assertEqual(hit["result"]["metrics"], {"combined_score": 0.25})
        self.assertEqual(hit["result"]["attempts"], 1)

        miss = self._send("work_lookup", key="unit-absent")
        self.assertIsNone(miss.get("result"))

        stats = self._send("cache_status")["result"]
        self.assertIn("work_cache", stats)
        self.assertEqual(stats["work_cache"]["hits"], 1)
        self.assertEqual(stats["work_cache"]["misses"], 1)

    def test_bad_request_fails_closed(self):
        bad = self._send("work_store", key="", metrics={"s": 1})
        self.assertIn("error", bad)
        bad_lookup = self._send("work_lookup", key="")
        self.assertIn("error", bad_lookup)


if __name__ == "__main__":
    unittest.main()
