"""Tests for mcp_github.py — stdlib-only GitHub MCP server protocol layer."""

import json
import os
import sys
import unittest
import tempfile
import io

# Isolated test env
test_dir = tempfile.mkdtemp()
os.environ["TOOLRECALL_CACHE_DB"] = os.path.join(test_dir, "test_gh.db")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from toolrecall.cache import _init


class TestMCPGithubProtocol(unittest.TestCase):
    """Test the GitHub MCP server's JSON-RPC protocol layer.
    
    NOTE: These tests validate protocol handling and schema, NOT live API calls.
    The GitHub REST API path (_api function) requires a real token and network.
    We test: tool listing, schema validation, token detection, JSON-RPC compliance.
    """

    def setUp(self):
        _init()

    def _run_github(self, request_str, env_token=""):
        """Run the github MCP server with ONE request and a fake token."""
        old_stdin = sys.stdin
        old_stdout = sys.stdout
        old_stderr = sys.stderr
        old_env = os.environ.get("GITHUB_TOKEN", "")

        stdin_buf = io.StringIO(request_str + "\n")
        stdout_buf = io.StringIO()
        stderr_buf = io.StringIO()

        # Set token so server initializes properly
        if env_token:
            os.environ["GITHUB_TOKEN"] = env_token
        elif "GITHUB_TOKEN" in os.environ:
            del os.environ["GITHUB_TOKEN"]

        # Re-import to pick up env changes
        import importlib
        from toolrecall import mcp_github
        importlib.reload(mcp_github)

        try:
            sys.stdin = stdin_buf
            sys.stdout = stdout_buf
            sys.stderr = stderr_buf
            mcp_github.main()
        except SystemExit:
            pass
        finally:
            sys.stdin = old_stdin
            sys.stdout = old_stdout
            sys.stderr = old_stderr
            if old_env:
                os.environ["GITHUB_TOKEN"] = old_env
            elif "GITHUB_TOKEN" in os.environ:
                del os.environ["GITHUB_TOKEN"]

        output = stdout_buf.getvalue().strip()
        if output:
            return json.loads(output)
        return None

    def test_initialize(self):
        """Prove initialize returns capabilities."""
        resp = self._run_github(
            '{"jsonrpc":"2.0","method":"initialize","id":1}',
            env_token="ghp_test123"
        )
        self.assertIsNotNone(resp)
        self.assertIn("result", resp)
        self.assertIn("capabilities", resp["result"])
        self.assertIn("tools", resp["result"]["capabilities"])

    def test_tools_list_returns_all_tools(self):
        """Prove tools/list returns expected tool names and schemas."""
        resp = self._run_github(
            '{"jsonrpc":"2.0","method":"tools/list","id":1}',
            env_token="ghp_test123"
        )
        tools = resp["result"]["tools"]
        names = [t["name"] for t in tools]
        self.assertEqual(len(tools), 5)
        self.assertIn("create_repository", names)
        self.assertIn("create_or_update_file", names)
        self.assertIn("push_files", names)
        self.assertIn("list_commits", names)
        self.assertIn("list_repos", names)

    def test_tool_schemas_all_have_required_fields(self):
        """Prove every tool has valid inputSchema with properties."""
        resp = self._run_github(
            '{"jsonrpc":"2.0","method":"tools/list","id":1}',
            env_token="ghp_test123"
        )
        for tool in resp["result"]["tools"]:
            self.assertIn("inputSchema", tool,
                          f"Tool '{tool['name']}' missing inputSchema")
            self.assertIn("properties", tool["inputSchema"],
                          f"Tool '{tool['name']}' missing properties")

    def test_unknown_tool_returns_error(self):
        """Prove unknown tool returns -32601."""
        resp = self._run_github(
            '{"jsonrpc":"2.0","method":"tools/call","id":1,'
            '"params":{"name":"nonexistent_tool","arguments":{}}}',
            env_token="ghp_test123"
        )
        self.assertEqual(resp["error"]["code"], -32601)

    def test_no_token_warning(self):
        """Prove server starts even without token but warns on stderr."""
        old_stdin = sys.stdin
        old_stdout = sys.stdout
        old_stderr = sys.stderr

        stdin_buf = io.StringIO()
        stdout_buf = io.StringIO()
        stderr_buf = io.StringIO()

        if "GITHUB_TOKEN" in os.environ:
            del os.environ["GITHUB_TOKEN"]
        if "GITHUB_PERSONAL_ACCESS_TOKEN" in os.environ:
            del os.environ["GITHUB_PERSONAL_ACCESS_TOKEN"]

        import importlib
        from toolrecall import mcp_github
        importlib.reload(mcp_github)

        try:
            sys.stdin = stdin_buf
            sys.stdout = stdout_buf
            sys.stderr = stderr_buf
            mcp_github.main()
        except SystemExit:
            pass
        finally:
            sys.stdin = old_stdin
            sys.stdout = old_stdout
            sys.stderr = old_stderr

        stderr_text = stderr_buf.getvalue()
        # Server warns on stderr when no token is set
        self.assertIn("No token", stderr_text)

    def test_tools_list_available_even_without_token(self):
        """Prove tools/list still works when no token is set."""
        resp = self._run_github(
            '{"jsonrpc":"2.0","method":"tools/list","id":1}',
            env_token=""
        )
        self.assertIsNotNone(resp)
        self.assertIn("result", resp)


if __name__ == "__main__":
    unittest.main()
