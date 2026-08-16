"""E2E: recall tier daemon roundtrip over the UDS.

Spawns a real daemon subprocess (with [recall] enabled), stores a
non-reproducible block via `recall_store`, then restores it via
`recall_get`. Verifies the full store → node_id → restore contract
through the daemon boundary.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tests.e2e_helpers import E2EDaemon  # noqa: E402


class TestE2ERecallDaemon(unittest.TestCase):
    """Store/get roundtrip through a real daemon."""

    def setUp(self):
        os.environ["TOOLRECALL_RECALL_ENABLED"] = "true"
        self.addCleanup(os.environ.pop, "TOOLRECALL_RECALL_ENABLED", None)
        self.daemon = E2EDaemon()
        self.daemon.start()

    def tearDown(self):
        self.daemon.stop()

    def test_recall_store_get_roundtrip(self):
        resp = self.daemon.client.send(
            {
                "cmd": "recall_store",
                "fingerprint": "e2e-fp",
                "content": "raw e2e output",
                "content_type": "web",
                "reproducible": False,
            }
        )
        self.assertNotIn("error", resp, f"store failed: {resp}")
        node_id_ = resp.get("node_id")
        self.assertTrue(node_id_)

        got = self.daemon.client.send({"cmd": "recall_get", "node_id": node_id_})
        self.assertNotIn("error", got, f"get failed: {got}")
        entry = got.get("entry")
        self.assertEqual(entry["content"], "raw e2e output")
        self.assertIs(entry["reproducible"], False)

    def test_recall_get_unknown_node_id_returns_none(self):
        got = self.daemon.client.send({"cmd": "recall_get", "node_id": "nope"})
        self.assertIsNone(got.get("entry"))


class TestE2ERecallBridgeVisibility(unittest.TestCase):
    """Bridge exposes recall tools only when the recall tier is enabled."""

    def setUp(self):
        self.daemon = E2EDaemon()
        os.environ["TOOLRECALL_RECALL_ENABLED"] = "true"
        self.daemon.start()

    def tearDown(self):
        os.environ.pop("TOOLRECALL_RECALL_ENABLED", None)
        self.daemon.stop()

    def _tool_names(self):
        from toolrecall.mcp_bridge import MCPBridge

        bridge = MCPBridge(socket_path=self.daemon.socket_path)
        res = bridge._handle_tools_list(None)
        return [t["name"] for t in res["result"]["tools"]]

    def test_recall_tools_listed_when_enabled(self):
        names = self._tool_names()
        self.assertIn("recall_store", names)
        self.assertIn("recall_get", names)

    def test_recall_tools_hidden_until_enabled(self):
        self.daemon.stop()
        os.environ.pop("TOOLRECALL_RECALL_ENABLED", None)
        self.daemon = E2EDaemon()
        self.daemon.start()
        names = self._tool_names()
        self.assertNotIn("recall_store", names)
        self.assertNotIn("recall_get", names)


if __name__ == "__main__":
    unittest.main()
