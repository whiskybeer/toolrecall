"""update_pending_hint daemon command + MCP bridge emission (integration).

Wires the updater's pending_update_hint() into the agent-facing channels:
the daemon serves {hint: ...} on request, the MCP bridge appends it to the
first tool result of a session (once per session client-side, once per
version daemon-side).
"""

import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from toolrecall import updater

_VER = updater._VERSION


class FakeCfg:
    update_enabled = False
    update_auto_apply = False
    update_check_interval_hours = 24.0
    mcp_allowed_paths = ["/tmp"]
    mcp_allow_terminal = False
    mcp_allowed_terminal_commands = []
    mcp_allow_invalidate = False
    mcp_multiplex_enabled = True
    mcp_multiplex_servers = []
    mcp_tool_access_control = False
    mcp_dangerous_tool_keywords = []
    mcp_cognitive_check_enabled = True
    mcp_ast_check_enabled = True
    mcp_allow_symlink_escapes = False
    mcp_client_policy = lambda self: {}  # noqa: E731


class TestDaemonUpdateHintHandler(unittest.TestCase):
    def _daemon(self):
        from toolrecall.daemon import DaemonServer

        with patch.object(updater, "_state_path") as sp:
            self.state_file = os.path.join(__import__("tempfile").mkdtemp(), "state.json")
            sp.return_value = self.state_file
            return DaemonServer(socket_path=os.path.join(self.state_file + ".sock"))

    def test_hint_served_and_marked(self):
        d = self._daemon()
        updater._save_state({"latest_seen": "9.9.9"})
        resp = d._handle_update_pending_hint({})
        self.assertIn("9.9.9", resp["hint"])
        # Throttled: second ask is silent.
        self.assertEqual(d._handle_update_pending_hint({})["hint"], "")

    def test_current_version_silent(self):
        d = self._daemon()
        updater._save_state({"latest_seen": _VER})
        self.assertEqual(d._handle_update_pending_hint({})["hint"], "")

    def test_never_raises(self):
        d = self._daemon()
        with patch.object(updater, "pending_update_hint", side_effect=RuntimeError("boom")):
            self.assertEqual(d._handle_update_pending_hint({})["hint"], "")


class TestBridgeUpdateHint(unittest.TestCase):
    """The MCP bridge appends the notice to the first tool result."""

    def _bridge_with_resp(self):
        from toolrecall.mcp_bridge import MCPBridge

        with patch("toolrecall.mcp_bridge.TransportClient") as tc:
            bridge = MCPBridge.__new__(MCPBridge)
            bridge.client = tc.return_value
            bridge._update_hint_done = False
            return bridge, tc

    def _bridge(self, side_effects):
        """A bridge built via __new__ with the full manual init the handler needs."""
        from toolrecall.mcp_bridge import MCPBridge

        with patch("toolrecall.mcp_bridge.TransportClient") as tc:
            client = tc.return_value
            client.send.side_effect = side_effects
            bridge = MCPBridge.__new__(MCPBridge)
            bridge.client = client
            bridge._start_time = 0.0
            bridge._update_hint_done = False
            bridge._emit_context_hints = False
            bridge._hints_opted_in = False
            bridge._nudge_reads = 0
            bridge._nudge_emitted = False
            bridge._multiplexer_only = False
            bridge._session_reads = {}
            bridge._last_full_send = {}
            bridge._consecutive_stubs = {}
            bridge._session_terminal = {}
            return bridge

    def test_first_tool_call_carries_hint(self):
        bridge = self._bridge(
            [
                {"cached": False, "content": "hello", "path": "/tmp/x"},
                {"hint": "[toolrecall] Update available: 0.8.21 → 9.9.9"},
            ]
        )
        out = bridge.handle_request(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "cached_read", "arguments": {"path": "/tmp/x"}},
            }
        )
        text = out["result"]["content"][0]["text"]
        self.assertIn("9.9.9", text)

    def test_hint_only_once_per_session(self):
        bridge = self._bridge(None)
        bridge.client.send.side_effect = lambda payload: (
            {"hint": "[toolrecall] Update available: 0.8.21 → 9.9.9"}
            if payload.get("cmd") == "update_pending_hint"
            else {"cached": False, "content": "hello", "path": "/tmp/x"}
        )
        req = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "cached_read", "arguments": {"path": "/tmp/x"}},
        }
        out1 = bridge.handle_request(dict(req, id=1))
        out2 = bridge.handle_request(dict(req, id=2))
        hint_calls = [
            c
            for c in bridge.client.send.call_args_list
            if c.args and c.args[0].get("cmd") == "update_pending_hint"
        ]
        self.assertEqual(len(hint_calls), 1)
        self.assertIn("9.9.9", out1["result"]["content"][0]["text"])
        self.assertNotIn("9.9.9", out2["result"]["content"][0]["text"])

    def test_daemon_error_never_breaks_tool_call(self):
        bridge = self._bridge(
            [
                {"cached": False, "content": "hello", "path": "/tmp/x"},
                RuntimeError("daemon gone"),
            ]
        )
        out = bridge.handle_request(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "cached_read", "arguments": {"path": "/tmp/x"}},
            }
        )
        self.assertIn("hello", out["result"]["content"][0]["text"])


if __name__ == "__main__":
    unittest.main()
