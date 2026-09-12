"""E2E: tracker persistence across daemon restart (option A).

Starts a real daemon subprocess, drives reads/checkpoint through the
socket, stops it (snapshot path), starts a second daemon on the SAME
db, and verifies context_get_stats reflects restored state instead of
the old per-restart reset.
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tests.e2e_helpers import E2EDaemon


class TestTrackerPersistenceAcrossRestart(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="trk_persist_e2e_")
        self.db_path = os.path.join(self._tmp.name, "cache.db")
        self.socket_path = os.path.join(self._tmp.name, "t.sock")
        self._daemons = []

    def tearDown(self):
        for d in self._daemons:
            try:
                d.stop()
            except Exception:
                pass
        self._tmp.cleanup()

    def _start(self) -> E2EDaemon:
        d = E2EDaemon(socket_path=self.socket_path, db_path=self.db_path)
        d.start()
        self._daemons.append(d)
        return d

    def test_state_survives_restart(self):
        d1 = self._start()
        c1 = d1._client

        # Drive the flow: read two files, set an agent checkpoint.
        f1 = os.path.join(self._tmp.name, "alpha.py")
        f2 = os.path.join(self._tmp.name, "beta.py")
        for p, body in ((f1, "a = 1\n"), (f2, "b = 2\n")):
            with open(p, "w") as f:
                f.write(body)
        r1 = c1.send({"cmd": "cached_read", "path": f1, "source": "agent_tool"})
        r2 = c1.send({"cmd": "cached_read", "path": f2, "source": "agent_tool"})
        self.assertFalse(r1.get("error"), r1)
        self.assertFalse(r2.get("error"), r2)
        cp = c1.send({"cmd": "context_set_checkpoint", "name": "e2e"})
        self.assertFalse(cp.get("error"), cp)

        stats_before = c1.send({"cmd": "context_get_stats"})
        self.assertEqual(stats_before["total_read"], 2)

        # Graceful stop → snapshot fires in stop().
        d1.stop()

        # Same db, fresh daemon: restore path in __init__.
        d2 = self._start()
        c2 = d2._client
        stats_after = c2.send({"cmd": "context_get_stats"})
        self.assertEqual(
            stats_after["total_read"],
            2,
            f"tracker state lost across restart: {stats_after}",
        )
        # The restored counter (2) plus daemon 2's startup checkpoint (1).
        self.assertEqual(stats_after["checkpoint"], stats_before["checkpoint"] + 1)
        self.assertTrue(stats_after["agent_armed"])

    def test_reset_then_restart_stays_empty(self):
        d1 = self._start()
        c1 = d1._client
        f1 = os.path.join(self._tmp.name, "gamma.py")
        with open(f1, "w") as f:
            f.write("g = 1\n")
        c1.send({"cmd": "cached_read", "path": f1, "source": "agent_tool"})
        c1.send({"cmd": "context_reset"})
        d1.stop()

        d2 = self._start()
        stats = d2._client.send({"cmd": "context_get_stats"})
        self.assertEqual(stats["total_read"], 0, f"reset row resurrected: {stats}")


if __name__ == "__main__":
    unittest.main()
