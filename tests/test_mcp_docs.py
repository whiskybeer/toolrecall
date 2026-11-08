"""Tests for mcp_docs.py — stdlib FTS5 knowledge-search MCP server.

Env isolation: the conftest autouse fixture pins TOOLRECALL_KNOWLEDGE_DB to
a per-test hermetic path (and disables docs auto-refresh), so this module
pins nothing itself and indexes into whatever DB the current env resolves.
"""

import io
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from toolrecall.mcp_docs import main as docs_main
from toolrecall.docs import index_directory


class TestMCPDocs(unittest.TestCase):
    """Test the MCP docs server's JSON-RPC protocol layer over a real index."""

    def setUp(self):
        self.doc_dir = tempfile.mkdtemp()
        with open(os.path.join(self.doc_dir, "alpha.md"), "w") as f:
            f.write("# Alpha\n\nThe quick brown fox jumps over the lazy dog.\n")
        with open(os.path.join(self.doc_dir, "beta.md"), "w") as f:
            f.write("# Beta\n\nCompletely unrelated content about databases.\n")
        n = index_directory(self.doc_dir, source="test-docs")
        assert n == 2, f"expected 2 pages indexed, got {n}"

    def _run(self, request):
        old_stdin, old_stdout, old_stderr = sys.stdin, sys.stdout, sys.stderr
        stdin_buf = io.StringIO(request)
        stdout_buf = io.StringIO()
        stderr_buf = io.StringIO()
        try:
            sys.stdin, sys.stdout, sys.stderr = stdin_buf, stdout_buf, stderr_buf
            docs_main()
        except SystemExit:
            pass
        finally:
            sys.stdin, sys.stdout, sys.stderr = old_stdin, old_stdout, old_stderr
        return stderr_buf.getvalue(), stdout_buf.getvalue()

    def _rpc(self, method, params=None, rid=1):
        stderr, stdout = self._run(
            json.dumps({"id": rid, "method": method, "params": params or {}}) + "\n"
        )
        lines = [line for line in stdout.strip().splitlines() if line.strip()]
        self.assertEqual(len(lines), 1, f"expected 1 response line, got: {stdout!r}")
        resp = json.loads(lines[-1])
        self.assertEqual(resp.get("id"), rid)
        return stderr, resp

    def test_startup_banner(self):
        stderr, stdout = self._run("")
        self.assertIn("ToolRecall Docs MCP Server", stderr)

    def test_initialize(self):
        _, resp = self._rpc("initialize", {})
        self.assertEqual(resp["result"]["serverInfo"]["name"], "toolrecall-docs")

    def test_tools_list(self):
        _, resp = self._rpc("tools/list", {})
        names = [t["name"] for t in resp["result"]["tools"]]
        self.assertEqual(names, ["docs_search", "docs_get_page"])

    def test_docs_search_finds_indexed_content(self):
        _, resp = self._rpc(
            "tools/call", {"name": "docs_search", "arguments": {"query": "quick brown fox"}}
        )
        text = json.loads(resp["result"]["content"][0]["text"])
        self.assertIsInstance(text, str, "docs tools return plain strings, not nested JSON")
        self.assertIn("Alpha", text)
        self.assertIn("quick", text)

    def test_docs_get_page_returns_content(self):
        _, resp = self._rpc(
            "tools/call",
            {"name": "docs_get_page", "arguments": {"source": "test-docs", "path": "beta.md"}},
        )
        text = json.loads(resp["result"]["content"][0]["text"])
        self.assertIn("databases", text)

    def test_unknown_tool(self):
        _, resp = self._rpc("tools/call", {"name": "nope", "arguments": {}})
        self.assertEqual(resp["error"]["code"], -32601)

    def test_unknown_method(self):
        _, resp = self._rpc("bogus/method", {})
        self.assertEqual(resp["error"]["code"], -32601)


if __name__ == "__main__":
    unittest.main()
