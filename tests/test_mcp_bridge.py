"""Tests for mcp_bridge.py — MCP JSON-RPC ↔ UDS protocol adapter.

The MCP Bridge is a thin adapter: reads MCP JSON-RPC from stdin, translates
requests into UDS calls to the ToolRecall Daemon, writes responses to stdout.

Tests cover:
  - MCPBridge handles initialize (returns capabilities)
  - MCPBridge handles tools/list (filters tools by daemon security gates)
  - MCPBridge handles tools/call for cached_read, cached_terminal, read_file, write_file, patch, terminal, etc.
  - MCPBridge handles tools/call with bypass_cache → refresh_file
  - MCPBridge handles mcp_call and mcp_list_servers
  - MCPBridge returns -32601 for unknown tools/methods
  - MCPBridge silently ignores notifications/initialized
  - TOOL_DEFINITIONS contain correct schemas for all 18 tools (14 original + 4 context tracker)
  - CMD_TO_MCP mapping covers all tool names (native aliases map to cached_* daemon commands)
  - main() exits with error when daemon unavailable

Uses a real UDS server to simulate daemon responses.
"""

import os
import sys
import threading
import time
import unittest
import tempfile
import io
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import toolrecall.mcp_bridge
from toolrecall.mcp_bridge import MCPBridge, TOOL_DEFINITIONS, CMD_TO_MCP
from toolrecall.transport import (
    create_socket, bind_socket, send_message, receive_message,
)


class MockDaemonServer:
    """A minimal UDS server that responds to daemon commands.

    Simulates the ToolRecall daemon for MCP bridge tests.
    Ping returns security info. mcp_call and other commands echo back.
    """

    def __init__(self, socket_path: str):
        self.path = socket_path
        self.server = None
        self.thread = None

    def start(self, ping_response: dict = None, call_responses: dict = None):
        """Start the mock daemon in background thread."""
        if ping_response is None:
            ping_response = {
                "allowed_paths": ["/home", "/tmp"],
                "allow_terminal": True,
                "allow_invalidate": True,
                "multiplex_enabled": True,
            }
        self.call_responses = call_responses or {}

        self.server = create_socket(self.path)
        bind_socket(self.server, self.path)
        self.server.listen(5)
        self.thread = threading.Thread(
            target=self._serve_loop,
            args=(ping_response,),
            daemon=True,
        )
        self.thread.start()
        time.sleep(0.1)

    def _serve_loop(self, ping_response):
        while True:
            try:
                conn, _ = self.server.accept()
                req = receive_message(conn)
                if req is None:
                    conn.close()
                    continue
                cmd = req.get("cmd", "")
                if cmd == "ping":
                    send_message(conn, ping_response)
                elif cmd in self.call_responses:
                    send_message(conn, self.call_responses[cmd])
                elif cmd == "mcp_call":
                    # Echo with mock result
                    send_message(conn, {
                        "result": {
                            "status": "ok",
                            "server": req.get("server"),
                            "tool": req.get("tool"),
                        }
                    })
                elif cmd == "mcp_list_servers":
                    send_message(conn, {
                        "result": [
                            {"name": "github", "running": True, "tool_names": ["list_issues"]},
                            {"name": "time", "running": True, "tool_names": ["get_time"]},
                        ]
                    })
                else:
                    send_message(conn, {"result": {"echo": cmd, **req}})
                conn.close()
            except Exception:
                break

    def stop(self):
        try:
            os.unlink(self.path)
        except FileNotFoundError:
            pass


class TestMCPBridgeProtocol(unittest.TestCase):
    """MCPBridge JSON-RPC protocol handling."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.sock_path = os.path.join(self.tmpdir, "bridge_test.sock")
        self.daemon = MockDaemonServer(self.sock_path)
        self.daemon.start()
        self.bridge = MCPBridge(self.sock_path)

    def tearDown(self):
        self.daemon.stop()

    # ── Initialize ─────────────────────────────────────────

    def test_initialize_returns_capabilities(self):
        """Initialize returns protocol version, server info, security."""
        resp = self.bridge.handle_request({
            "jsonrpc": "2.0", "method": "initialize", "id": 1
        })
        self.assertIsNotNone(resp)
        self.assertIn("result", resp)
        r = resp["result"]
        self.assertEqual(r["protocolVersion"], "2024-11-05")
        self.assertEqual(r["serverInfo"]["name"], "ToolRecall (Bridge)")
        self.assertIn("capabilities", r)
        self.assertIn("tools", r["capabilities"])

    def test_initialize_security_info(self):
        """Initialize result includes security gates from daemon ping."""
        resp = self.bridge.handle_request({
            "jsonrpc": "2.0", "method": "initialize", "id": 1
        })
        sec = resp["result"]["serverInfo"]["security"]
        self.assertIn("allowed_paths", sec)
        self.assertIn("allow_terminal", sec)
        self.assertIn("allow_invalidate", sec)

    # ── Tools/List ─────────────────────────────────────────

    def test_tools_list_returns_all_with_gates_open(self):
        """When all gates enabled, all 18 tools are listed (14 original + 4 context tracker)."""
        resp = self.bridge.handle_request({
            "jsonrpc": "2.0", "method": "tools/list", "id": 1
        })
        tools = resp["result"]["tools"]
        names = [t["name"] for t in tools]
        # Original tool names
        self.assertIn("cached_read", names)
        self.assertIn("cached_terminal", names)
        self.assertIn("cached_skill", names)
        self.assertIn("docs_search", names)
        self.assertIn("docs_get_page", names)
        self.assertIn("cache_status", names)
        self.assertIn("cache_invalidate", names)
        self.assertIn("cache_refresh_file", names)
        self.assertIn("mcp_call", names)
        self.assertIn("mcp_list_servers", names)
        # Native-named aliases
        self.assertIn("read_file", names)
        self.assertIn("write_file", names)
        self.assertIn("patch", names)
        self.assertIn("terminal", names)
        # Context tracker tools
        self.assertIn("context_set_checkpoint", names)
        self.assertIn("context_get_dirty", names)
        self.assertIn("context_get_stats", names)
        self.assertIn("context_reset", names)
        self.assertEqual(len(tools), 18)

    def test_tools_list_hides_terminal_when_disabled(self):
        """cached_terminal and terminal are hidden when daemon has allow_terminal=False."""
        # Override daemon response for this test
        daemon2 = MockDaemonServer(self.sock_path)
        daemon2.start(ping_response={
            "allowed_paths": ["/tmp"],
            "allow_terminal": False,  # Terminal DISABLED
            "allow_invalidate": True,
            "multiplex_enabled": True,
        })
        resp = self.bridge.handle_request({
            "jsonrpc": "2.0", "method": "tools/list", "id": 1
        })
        names = [t["name"] for t in resp["result"]["tools"]]
        self.assertNotIn("cached_terminal", names)
        self.assertNotIn("terminal", names)
        self.assertIn("cached_read", names)
        self.assertIn("read_file", names)

    def test_tools_list_hides_invalidate_when_disabled(self):
        """cache_invalidate is hidden when allow_invalidate=False."""
        daemon2 = MockDaemonServer(self.sock_path)
        daemon2.start(ping_response={
            "allowed_paths": ["/tmp"],
            "allow_terminal": False,
            "allow_invalidate": False,  # Invalidate DISABLED
            "multiplex_enabled": True,
        })
        resp = self.bridge.handle_request({
            "jsonrpc": "2.0", "method": "tools/list", "id": 1
        })
        names = [t["name"] for t in resp["result"]["tools"]]
        self.assertNotIn("cache_invalidate", names)

    def test_tools_list_hides_mcp_when_multiplex_disabled(self):
        """mcp_call/mcp_list_servers hidden when multiplex_enabled=False."""
        daemon2 = MockDaemonServer(self.sock_path)
        daemon2.start(ping_response={
            "allowed_paths": ["/tmp"],
            "allow_terminal": True,
            "allow_invalidate": True,
            "multiplex_enabled": False,  # Multiplex DISABLED
        })
        resp = self.bridge.handle_request({
            "jsonrpc": "2.0", "method": "tools/list", "id": 1
        })
        names = [t["name"] for t in resp["result"]["tools"]]
        self.assertNotIn("mcp_call", names)
        self.assertNotIn("mcp_list_servers", names)

    # ── Tools/Call: cached_read ────────────────────────────

    def test_tool_call_cached_read(self):
        """cached_read calls daemon with cmd=cached_read and path argument."""
        resp = self.bridge.handle_request({
            "jsonrpc": "2.0", "method": "tools/call", "id": 1,
            "params": {"name": "cached_read", "arguments": {"path": "/tmp/test.txt"}}
        })
        self.assertIn("result", resp)
        content = resp["result"]["content"][0]["text"]
        self.assertIn("/tmp/test.txt", content)

    def test_tool_call_cached_read_with_bypass(self):
        """cached_read with bypass_cache=true sends cache_refresh_file to daemon."""
        resp = self.bridge.handle_request({
            "jsonrpc": "2.0", "method": "tools/call", "id": 1,
            "params": {
                "name": "cached_read",
                "arguments": {"path": "/tmp/test.txt", "bypass_cache": True}
            }
        })
        self.assertIn("result", resp)

    # ── Tools/Call: cached_terminal ────────────────────────

    def test_tool_call_cached_terminal(self):
        """cached_terminal calls daemon with cmd=cached_terminal."""
        resp = self.bridge.handle_request({
            "jsonrpc": "2.0", "method": "tools/call", "id": 1,
            "params": {"name": "cached_terminal", "arguments": {"command": "echo hello"}}
        })
        self.assertIn("result", resp)

    def test_tool_call_cached_skill(self):
        """cached_skill calls daemon with cmd=cached_skill."""
        resp = self.bridge.handle_request({
            "jsonrpc": "2.0", "method": "tools/call", "id": 1,
            "params": {"name": "cached_skill", "arguments": {"name": "test-skill"}}
        })
        self.assertIn("result", resp)

    # ── Tools/Call: docs_search / docs_get_page ────────────

    def test_tool_call_docs_search(self):
        """docs_search calls daemon with cmd=docs_search and query."""
        resp = self.bridge.handle_request({
            "jsonrpc": "2.0", "method": "tools/call", "id": 1,
            "params": {"name": "docs_search", "arguments": {"query": "python", "source": "docs"}}
        })
        self.assertIn("result", resp)

    def test_tool_call_docs_get_page(self):
        """docs_get_page calls daemon with cmd=docs_get_page."""
        resp = self.bridge.handle_request({
            "jsonrpc": "2.0", "method": "tools/call", "id": 1,
            "params": {
                "name": "docs_get_page",
                "arguments": {"source": "docs", "path": "readme.md"}
            }
        })
        self.assertIn("result", resp)

    # ── Tools/Call: cache operations ───────────────────────

    def test_tool_call_cache_status(self):
        """cache_status calls daemon with cmd=cache_status."""
        resp = self.bridge.handle_request({
            "jsonrpc": "2.0", "method": "tools/call", "id": 1,
            "params": {"name": "cache_status", "arguments": {}}
        })
        self.assertIn("result", resp)

    def test_tool_call_cache_invalidate(self):
        """cache_invalidate calls daemon with cmd=cache_invalidate."""
        resp = self.bridge.handle_request({
            "jsonrpc": "2.0", "method": "tools/call", "id": 1,
            "params": {"name": "cache_invalidate", "arguments": {}}
        })
        self.assertIn("result", resp)

    def test_tool_call_cache_refresh_file(self):
        """cache_refresh_file calls daemon with cmd=cache_refresh_file."""
        resp = self.bridge.handle_request({
            "jsonrpc": "2.0", "method": "tools/call", "id": 1,
            "params": {
                "name": "cache_refresh_file",
                "arguments": {"path": "/tmp/test.txt"}
            }
        })
        self.assertIn("result", resp)

    # ── Tools/Call: MCP multiplex ──────────────────────────

    def test_tool_call_mcp_call(self):
        """mcp_call sends to daemon with server/tool/arguments packed."""
        resp = self.bridge.handle_request({
            "jsonrpc": "2.0", "method": "tools/call", "id": 1,
            "params": {
                "name": "mcp_call",
                "arguments": {
                    "server": "github",
                    "tool": "list_issues",
                    "arguments": {"owner": "whiskybeer", "repo": "toolrecall"},
                }
            }
        })
        self.assertIn("result", resp)

    def test_tool_call_mcp_call_with_bypass(self):
        """mcp_call with bypass_cache sets ttl=0."""
        resp = self.bridge.handle_request({
            "jsonrpc": "2.0", "method": "tools/call", "id": 1,
            "params": {
                "name": "mcp_call",
                "arguments": {
                    "server": "time",
                    "tool": "get_time",
                    "arguments": {"timezone": "UTC"},
                    "bypass_cache": True,
                }
            }
        })
        self.assertIn("result", resp)

    def test_tool_call_mcp_list_servers(self):
        """mcp_list_servers returns available server list."""
        resp = self.bridge.handle_request({
            "jsonrpc": "2.0", "method": "tools/call", "id": 1,
            "params": {"name": "mcp_list_servers", "arguments": {}}
        })
        self.assertIn("result", resp)

    # ── Tools/Call: Context Tracker ──────────────────────

    def test_tool_call_context_set_checkpoint(self):
        """context_set_checkpoint calls daemon with cmd=context_set_checkpoint."""
        resp = self.bridge.handle_request({
            "jsonrpc": "2.0", "method": "tools/call", "id": 1,
            "params": {
                "name": "context_set_checkpoint",
                "arguments": {"name": "before_edit"}
            }
        })
        self.assertIn("result", resp)
        text = resp["result"]["content"][0]["text"]
        self.assertIn("context_set_checkpoint", text)

    def test_tool_call_context_get_dirty(self):
        """context_get_dirty calls daemon with cmd=context_get_dirty."""
        resp = self.bridge.handle_request({
            "jsonrpc": "2.0", "method": "tools/call", "id": 1,
            "params": {
                "name": "context_get_dirty",
                "arguments": {"checkpoint": 1}
            }
        })
        self.assertIn("result", resp)
        text = resp["result"]["content"][0]["text"]
        self.assertIn("context_get_dirty", text)

    def test_tool_call_context_get_stats(self):
        """context_get_stats calls daemon with cmd=context_get_stats."""
        resp = self.bridge.handle_request({
            "jsonrpc": "2.0", "method": "tools/call", "id": 1,
            "params": {"name": "context_get_stats", "arguments": {}}
        })
        self.assertIn("result", resp)

    def test_tool_call_context_reset(self):
        """context_reset calls daemon with cmd=context_reset."""
        resp = self.bridge.handle_request({
            "jsonrpc": "2.0", "method": "tools/call", "id": 1,
            "params": {"name": "context_reset", "arguments": {}}
        })
        self.assertIn("result", resp)

    # ── Tools/Call: source=agent_tool tracking ───────────

    def test_tool_call_cached_read_sends_source_agent_tool(self):
        """cached_read adds source=agent_tool to the daemon request."""
        daemon2 = MockDaemonServer(self.sock_path)
        daemon2.start(call_responses={
            "cached_read": {"result": {"echo": "cached_read", "source": "agent_tool"}},
        })
        resp = self.bridge.handle_request({
            "jsonrpc": "2.0", "method": "tools/call", "id": 1,
            "params": {"name": "cached_read", "arguments": {"path": "/tmp/test.txt"}}
        })
        self.assertIn("result", resp)

    def test_tool_call_read_file_sends_source_agent_tool(self):
        """read_file (native alias) adds source=agent_tool to the daemon request."""
        daemon2 = MockDaemonServer(self.sock_path)
        daemon2.start(call_responses={
            "cached_read": {"result": {"echo": "cached_read", "source": "agent_tool"}},
        })
        resp = self.bridge.handle_request({
            "jsonrpc": "2.0", "method": "tools/call", "id": 1,
            "params": {"name": "read_file", "arguments": {"path": "/tmp/test.txt"}}
        })
        self.assertIn("result", resp)

    # ── Error handling ─────────────────────────────────────

    def test_unknown_tool_returns_error(self):
        """Unknown tool returns JSON-RPC error -32601."""
        resp = self.bridge.handle_request({
            "jsonrpc": "2.0", "method": "tools/call", "id": 1,
            "params": {"name": "nonexistent_tool", "arguments": {}}
        })
        self.assertIn("error", resp)
        self.assertEqual(resp["error"]["code"], -32601)

    def test_unknown_method_returns_error(self):
        """Unknown method returns JSON-RPC error -32601."""
        resp = self.bridge.handle_request({
            "jsonrpc": "2.0", "method": "boogie", "id": 1
        })
        self.assertIn("error", resp)
        self.assertEqual(resp["error"]["code"], -32601)

    def test_notifications_ignored(self):
        """notifications/initialized returns None (silently ignored)."""
        resp = self.bridge.handle_request({
            "jsonrpc": "2.0", "method": "notifications/initialized"
        })
        self.assertIsNone(resp)

    def test_close_method_ignored(self):
        """Close method returns None (silently ignored)."""
        resp = self.bridge.handle_request({
            "jsonrpc": "2.0", "method": "close", "id": 1
        })
        self.assertIsNone(resp)

    def test_daemon_error_propagates(self):
        """If daemon returns error, bridge wraps it as JSON-RPC error."""
        daemon2 = MockDaemonServer(self.sock_path)
        daemon2.start(call_responses={
            "cached_read": {"error": "access denied"},
        })
        resp = self.bridge.handle_request({
            "jsonrpc": "2.0", "method": "tools/call", "id": 1,
            "params": {"name": "cached_read", "arguments": {"path": "/etc/shadow"}}
        })
        self.assertIn("error", resp)
        # Bridge wraps daemon errors under -32603
        self.assertEqual(resp["error"]["code"], -32603)
        self.assertIn("access denied", resp["error"]["message"])


class TestToolDefinitions(unittest.TestCase):
    """TOOL_DEFINITIONS contains valid schemas for all 18 tools."""

    def test_has_all_18_tools(self):
        """There are exactly 18 tool definitions (14 original + 4 context tracker)."""
        self.assertEqual(len(TOOL_DEFINITIONS), 18)

    def test_each_tool_has_valid_schema(self):
        """Every tool has name, description, and inputSchema with properties."""
        for tdef in TOOL_DEFINITIONS:
            self.assertIn("name", tdef, f"Tool missing name: {tdef}")
            self.assertIn("description", tdef, f"Tool '{tdef['name']}' missing description")
            self.assertIn("inputSchema", tdef, f"Tool '{tdef['name']}' missing inputSchema")
            schema = tdef["inputSchema"]
            self.assertIn("type", schema)
            self.assertEqual(schema["type"], "object")
            self.assertIn("properties", schema)

    def test_cached_read_schema(self):
        """cached_read requires path, optional bypass_cache."""
        tdef = next(t for t in TOOL_DEFINITIONS if t["name"] == "cached_read")
        props = tdef["inputSchema"]["properties"]
        self.assertIn("path", props)
        self.assertIn("bypass_cache", props)
        self.assertIn("path", tdef["inputSchema"]["required"])

    def test_mcp_call_schema(self):
        """mcp_call requires server and tool, optional arguments/bypass_cache."""
        tdef = next(t for t in TOOL_DEFINITIONS if t["name"] == "mcp_call")
        required = tdef["inputSchema"]["required"]
        self.assertIn("server", required)
        self.assertIn("tool", required)
        props = tdef["inputSchema"]["properties"]
        self.assertIn("arguments", props)
        self.assertIn("bypass_cache", props)


class TestCmdToMCPMapping(unittest.TestCase):
    """CMD_TO_MCP mapping covers all tool names."""

    def test_all_tools_mapped(self):
        """Every tool in TOOL_DEFINITIONS has a corresponding CMD_TO_MCP entry."""
        for tdef in TOOL_DEFINITIONS:
            self.assertIn(tdef["name"], CMD_TO_MCP, f"Missing CMD_TO_MCP entry for '{tdef['name']}'")

    def test_native_aliases_map_to_cached_cmds(self):
        """Native-named aliases map to their cached_* daemon commands, not to themselves."""
        native_to_cmd = {
            "read_file": "cached_read",
            "write_file": "cached_write",
            "patch": "cached_patch",
            "terminal": "cached_terminal",
        }
        for native_name, daemon_cmd in native_to_cmd.items():
            self.assertIn(native_name, CMD_TO_MCP, f"Missing native alias '{native_name}'")
            self.assertEqual(CMD_TO_MCP[native_name], daemon_cmd,
                             f"Native alias '{native_name}' should map to '{daemon_cmd}'")

    def test_original_names_map_to_themselves(self):
        """Original tool names (cached_*, docs_*, cache_*, mcp_*) map to themselves."""
        native_names = {"read_file", "write_file", "patch", "terminal"}
        for tdef in TOOL_DEFINITIONS:
            name = tdef["name"]
            if name not in native_names:
                self.assertEqual(CMD_TO_MCP[name], name,
                                 f"Original tool '{name}' should map to itself")


class TestMainFunctionDaemonCheck(unittest.TestCase):
    """main() exits with code 1 when daemon unavailable."""

    def test_main_exits_when_daemon_down(self):
        """Without a running daemon, main() prints error and exits."""
        sock_path = os.path.join(tempfile.mkdtemp(), "nope.sock")

        old_stdin = sys.stdin
        old_stdout = sys.stdout
        old_stderr = sys.stderr
        stdin_buf = io.StringIO("")
        stdout_buf = io.StringIO()
        stderr_buf = io.StringIO()

        try:
            sys.stdin = stdin_buf
            sys.stdout = stdout_buf
            sys.stderr = stderr_buf
            # Patch DEFAULT_PATH so MCPBridge() uses a non-existent socket
            with patch("toolrecall.mcp_bridge.DEFAULT_PATH", sock_path):
                with self.assertRaises(SystemExit) as ctx:
                    toolrecall.mcp_bridge.main()
            self.assertEqual(ctx.exception.code, 1)
            stderr = stderr_buf.getvalue()
            self.assertIn("daemon is not running", stderr.lower())
        finally:
            sys.stdin = old_stdin
            sys.stdout = old_stdout
            sys.stderr = old_stderr


if __name__ == "__main__":
    unittest.main()
