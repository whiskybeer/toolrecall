"""Compaction-epoch contract on the MCP bridge (consumer side).

The daemon stamps ``compaction_epoch`` on read/terminal responses; a harness
reports a transcript compaction through the daemon command ``context_compacted``
(Hermes calls it from its compression boundary). The bridge must then void its
session-scoped file-stub state, or a later "File unchanged; see earlier read"
stub points at content the summary just dropped.

These tests pin the consumer contract ``_sync_epoch`` implements — the half that
had no test while the producer side (daemon cmd ``context_compacted``) did:

  * the first epoch seen is recorded and never resets
  * the same epoch does not reset
  * a CHANGED epoch resets the stub state
  * a missing epoch / non-dict body is ignored
  * the field never reaches the agent
  * end-to-end: stub -> epoch change -> the next read serves full content

Hermetic: no daemon, no socket. ``_uds_request`` is the only outbound path a
read triggers (the measured-tokens counter) and is patched out.
"""

import os
import sys
import unittest
from unittest.mock import Mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from toolrecall.mcp_bridge import MCPBridge  # noqa: E402

SOCKET = "/nonexistent-toolrecall-compaction-epoch.sock"


class TestSyncEpoch(unittest.TestCase):
    """`_sync_epoch` in isolation."""

    def setUp(self):
        self.bridge = MCPBridge(socket_path=SOCKET)
        self.bridge._uds_request = lambda *a, **k: {}

    def test_first_epoch_is_recorded_without_reset(self):
        reset = Mock()
        self.bridge._stubs.reset_all = reset

        self.bridge._sync_epoch({"compaction_epoch": 7})

        self.assertEqual(self.bridge._compaction_epoch, 7)
        reset.assert_not_called()

    def test_same_epoch_does_not_reset(self):
        self.bridge._sync_epoch({"compaction_epoch": 7})
        reset = Mock()
        self.bridge._stubs.reset_all = reset

        self.bridge._sync_epoch({"compaction_epoch": 7})

        reset.assert_not_called()
        self.assertEqual(self.bridge._compaction_epoch, 7)

    def test_changed_epoch_resets_stub_state(self):
        self.bridge._sync_epoch({"compaction_epoch": 7})
        reset = Mock()
        self.bridge._stubs.reset_all = reset

        self.bridge._sync_epoch({"compaction_epoch": 8})

        reset.assert_called_once()
        self.assertEqual(self.bridge._compaction_epoch, 8)

    def test_missing_epoch_is_ignored(self):
        reset = Mock()
        self.bridge._stubs.reset_all = reset

        self.bridge._sync_epoch({"content": "no epoch here"})

        self.assertIsNone(self.bridge._compaction_epoch)
        reset.assert_not_called()

    def test_non_dict_body_is_ignored(self):
        # A non-dict response must not raise (defensive isinstance guard).
        self.bridge._sync_epoch(None)  # type: ignore[arg-type]

        self.assertIsNone(self.bridge._compaction_epoch)


class TestEpochOnReadPath(unittest.TestCase):
    """The read presenter: the field is stripped, and a boundary re-serves full."""

    PATH = "/tmp/toolrecall-epoch-probe.txt"
    BODY = "line one\nline two\nline three\n"

    def setUp(self):
        self.bridge = MCPBridge(socket_path=SOCKET)
        self.bridge._uds_request = lambda *a, **k: {}

    def _read(self, epoch):
        resp = {
            "result": {
                "path": self.PATH,
                "content": self.BODY,
                "compaction_epoch": epoch,
            }
        }
        return self.bridge._present_read({"path": self.PATH}, resp)

    def test_epoch_field_never_reaches_the_agent(self):
        out = self._read(3)

        self.assertNotIn("compaction_epoch", out["result"])

    def test_stub_then_epoch_change_serves_full_content_again(self):
        first = self._read(1)
        self.assertEqual(first["result"]["content"], self.BODY)  # full send

        second = self._read(1)
        self.assertTrue(second["result"].get("unchanged"))  # stub

        third = self._read(2)  # harness reported a compaction
        self.assertNotIn("unchanged", third["result"])
        self.assertEqual(third["result"]["content"], self.BODY)  # full again


if __name__ == "__main__":
    unittest.main()
