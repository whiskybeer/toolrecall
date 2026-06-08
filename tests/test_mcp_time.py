"""Tests for mcp_time.py — stdlib-only Python time MCP server."""

import json
import os
import sys
import unittest
import tempfile

# Isolated test env
test_dir = tempfile.mkdtemp()
os.environ["TOOLRECALL_CACHE_DB"] = os.path.join(test_dir, "test_time.db")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from toolrecall.mcp_time import main as time_main
from toolrecall.cache import _init


class TestMCPTime(unittest.TestCase):
    """Test the MCP time server's JSON-RPC protocol layer."""

    def setUp(self):
        _init()
        self.responses = []

    def _run(self, request):
        """Feed a JSON-RPC request to stdin, capture stdout response."""
        import io
        old_stdin = sys.stdin
        old_stdout = sys.stdout
        old_stderr = sys.stderr

        stdin_buf = io.StringIO(request)
        stdout_buf = io.StringIO()
        stderr_buf = io.StringIO()

        try:
            sys.stdin = stdin_buf
            sys.stdout = stdout_buf
            sys.stderr = stderr_buf
            time_main()
        except SystemExit:
            pass
        finally:
            sys.stdin = old_stdin
            sys.stdout = old_stdout
            sys.stderr = old_stderr

        output = stdout_buf.getvalue().strip()
        if output:
            return json.loads(output)
        return None

    def _call(self, method, params=None, req_id=1):
        """Build and parse a single JSON-RPC call."""
        req = {"jsonrpc": "2.0", "method": method, "id": req_id}
        if params is not None:
            req["params"] = params
        return self._run(json.dumps(req))

    def test_initialize(self):
        """Prove the server responds to initialize with capabilities."""
        resp = self._call("initialize")
        self.assertIsNotNone(resp)
        self.assertIn("result", resp)
        self.assertIn("capabilities", resp["result"])
        self.assertIn("tools", resp["result"]["capabilities"])

    def test_tools_list(self):
        """Prove tools/list returns correct tool schemas."""
        resp = self._call("tools/list")
        self.assertIsNotNone(resp)
        tools = resp.get("result", {}).get("tools", [])
        tool_names = [t["name"] for t in tools]
        self.assertIn("get_time", tool_names)
        self.assertIn("list_timezones", tool_names)

    def test_get_time_utc(self):
        """Prove get_time returns valid datetime for UTC.
        Server returns datetime with space separator (not ISO T)."""
        resp = self._call("tools/call", {
            "name": "get_time",
            "arguments": {"timezone": "UTC"}
        })
        self.assertIsNotNone(resp)
        content = resp.get("result", {}).get("content", [])
        text = json.loads(content[0]["text"])
        self.assertIn("timezone", text)
        self.assertEqual(text["timezone"], "UTC")
        self.assertIn("datetime", text)
        self.assertRegex(text["datetime"], r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}")

    def test_get_time_gmt(self):
        """Prove get_time returns GMT timezone."""
        resp = self._call("tools/call", {
            "name": "get_time",
            "arguments": {"timezone": "GMT"}
        })
        self.assertIsNotNone(resp)
        content = resp.get("result", {}).get("content", [])
        text = json.loads(content[0]["text"])
        self.assertIn("timezone", text)
        self.assertEqual(text["timezone"], "GMT")

    def test_get_time_est(self):
        """Prove get_time handles EST (negative offset zone)."""
        resp = self._call("tools/call", {
            "name": "get_time",
            "arguments": {"timezone": "EST"}
        })
        self.assertIsNotNone(resp)
        content = resp.get("result", {}).get("content", [])
        text = json.loads(content[0]["text"])
        self.assertIn("timezone", text)
        self.assertEqual(text["timezone"], "EST")

    def test_get_time_pst(self):
        """Prove get_time handles PST (negative offset)."""
        resp = self._call("tools/call", {
            "name": "get_time",
            "arguments": {"timezone": "PST"}
        })
        self.assertIsNotNone(resp)
        content = resp.get("result", {}).get("content", [])
        text = json.loads(content[0]["text"])
        self.assertIn("timezone", text)
        self.assertEqual(text["timezone"], "PST")

    def test_get_time_unknown_zone_returns_error(self):
        """Prove unknown IANA-style zone names return error in result text."""
        resp = self._call("tools/call", {
            "name": "get_time",
            "arguments": {"timezone": "Europe/Berlin"}
        })
        self.assertIsNotNone(resp)
        content = resp.get("result", {}).get("content", [])
        text = json.loads(content[0]["text"])
        # Error is wrapped in result content, not JSON-RPC error level
        self.assertIn("error", text)
        self.assertIn("Unknown timezone", text["error"])

    def test_list_timezones(self):
        """Prove list_timezones returns dict with timezones list."""
        resp = self._call("tools/call", {
            "name": "list_timezones",
            "arguments": {}
        })
        self.assertIsNotNone(resp)
        content = resp.get("result", {}).get("content", [])
        data = json.loads(content[0]["text"])
        self.assertIsInstance(data, dict)
        zones = data.get("timezones", [])
        self.assertGreaterEqual(len(zones), 20)
        self.assertIn("UTC", zones)
        self.assertIn("EST", zones)

    def test_missing_timezone_uses_default(self):
        """Prove missing timezone argument defaults to UTC."""
        resp = self._call("tools/call", {
            "name": "get_time",
            "arguments": {}
        })
        self.assertIsNotNone(resp)
        content = resp.get("result", {}).get("content", [])
        text = json.loads(content[0]["text"])
        self.assertEqual(text["timezone"], "UTC")

    def test_unknown_tool_returns_error(self):
        """Prove unknown tool names return -32601 error."""
        resp = self._call("tools/call", {
            "name": "nonexistent_tool",
            "arguments": {}
        })
        self.assertIsNotNone(resp)
        self.assertIn("error", resp)
        self.assertEqual(resp["error"]["code"], -32601)


if __name__ == "__main__":
    unittest.main()
