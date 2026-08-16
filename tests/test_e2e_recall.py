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

    def test_recall_stats_reports_entries(self):
        self.daemon.client.send(
            {
                "cmd": "recall_store",
                "fingerprint": "fp1",
                "content": "some content here",
                "content_type": "web",
                "reproducible": False,
            }
        )
        st = self.daemon.client.send({"cmd": "recall_stats"})
        self.assertEqual(st["total"], 1)
        self.assertGreater(st["tokens"], 0)

    def test_recall_get_accounting_recorded_in_cache_stats(self):
        import sqlite3

        node_id_ = self.daemon.client.send(
            {
                "cmd": "recall_store",
                "fingerprint": "fp-acct",
                "content": "accounted content",
                "content_type": "web",
                "reproducible": False,
            }
        )["node_id"]
        self.daemon.client.send({"cmd": "recall_get", "node_id": node_id_})

        conn = sqlite3.connect(self.daemon.db_path)
        try:
            row = conn.execute(
                "SELECT hits, tokens_saved, context_tokens_saved "
                "FROM cache_stats WHERE category='recall'"
            ).fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(row, "expected a cache_stats row for category 'recall'")
        self.assertEqual(row[0], 1, "one get hit should be recorded")
        self.assertGreater(row[2], 0, "context_tokens_saved should be > 0")


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


class TestE2ERecallCLI(unittest.TestCase):
    """`toolrecall context recall store/get` roundtrip against a real daemon."""

    def setUp(self):
        os.environ["TOOLRECALL_RECALL_ENABLED"] = "true"
        self.addCleanup(os.environ.pop, "TOOLRECALL_RECALL_ENABLED", None)
        self.daemon = E2EDaemon()
        self.daemon.start()
        from toolrecall import client as _client

        _client.set_socket_path(self.daemon.socket_path)
        self.addCleanup(_client.set_socket_path, "")

    def tearDown(self):
        self.daemon.stop()

    def test_cli_store_then_get_roundtrip(self):
        import contextlib
        import io
        import sys as _sys
        from unittest import mock

        from toolrecall import cli

        # store: pipe content via stdin
        _sys.argv = ["toolrecall", "context", "recall", "store", "fp-cli", "web"]
        out = io.StringIO()
        with (
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(io.StringIO()),
            mock.patch.object(cli.sys, "stdin", io.StringIO("cli raw content")),
        ):
            cli.cmd_context()
        node_id = out.getvalue().strip()
        self.assertTrue(node_id, "expected a node_id from recall store")

        # get: restore the content
        _sys.argv = ["toolrecall", "context", "recall", "get", node_id]
        out2 = io.StringIO()
        with contextlib.redirect_stdout(out2):
            cli.cmd_context()
        self.assertIn("cli raw content", out2.getvalue())

    def test_cli_recall_status_counts_entries(self):
        import contextlib
        import io
        import sys as _sys
        from unittest import mock

        from toolrecall import cli

        # seed one entry via the CLI store path
        _sys.argv = ["toolrecall", "context", "recall", "store", "fp-status", "web"]
        with (
            contextlib.redirect_stdout(io.StringIO()),
            mock.patch.object(cli.sys, "stdin", io.StringIO("status content")),
        ):
            cli.cmd_context()

        _sys.argv = ["toolrecall", "context", "recall", "status"]
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.cmd_context()
        self.assertIn("recall entries:", out.getvalue())
        self.assertIn("persisted tokens:", out.getvalue())


if __name__ == "__main__":
    unittest.main()
