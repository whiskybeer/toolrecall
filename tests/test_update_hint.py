"""Update-availability hint channel (2026-09 review, auto_apply = false).

With install-on-detect off by default, the notice must reach the primary
audience — agent-driven users who never run the CLI. Channels:

1. CLI stdout (test_updater.py: notify-only branch)
2. MCP bridge tool-result hint — updater.pending_update_hint() via the
   daemon's update_pending_hint command, emitted once per version per
   session; the daemon-side notified_for throttle also caps repeats
   across sessions.

Security note: the hint text contains only version numbers and the fix
command — no paths, no user data (hint strings are injected into agent
context).
"""

import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from toolrecall import updater


class TestPendingUpdateHint(unittest.TestCase):
    def setUp(self):
        self._tmp = __import__("tempfile").mkdtemp()
        self._state_patch = patch.object(
            updater, "_state_path", lambda: os.path.join(self._tmp, "updater_state.json")
        )
        self._state_patch.start()
        self.addCleanup(self._state_patch.stop)

    def test_current_version_no_hint(self):
        updater._save_state({"latest_seen": updater._VERSION})
        self.assertEqual(updater.pending_update_hint(), "")

    def test_no_state_no_hint(self):
        self.assertEqual(updater.pending_update_hint(), "")

    def test_older_seen_no_hint(self):
        updater._save_state({"latest_seen": "0.0.1"})
        self.assertEqual(updater.pending_update_hint(), "")

    def test_newer_seen_hint_contains_versions_and_command(self):
        updater._save_state({"latest_seen": "9.9.9"})
        hint = updater.pending_update_hint()
        self.assertIn(updater._VERSION, hint)
        self.assertIn("9.9.9", hint)
        self.assertIn("toolrecall update", hint)

    def test_hint_once_per_version(self):
        updater._save_state({"latest_seen": "9.9.9"})
        first = updater.pending_update_hint()
        self.assertTrue(first)
        updater.mark_update_notified("9.9.9")
        self.assertEqual(updater.pending_update_hint(), "")

    def test_new_version_renotifies_after_notified(self):
        updater._save_state({"latest_seen": "9.9.9", "notified_for": "9.9.8"})
        self.assertTrue(updater.pending_update_hint())


if __name__ == "__main__":
    unittest.main()
