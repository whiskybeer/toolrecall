"""Tests for client.py — ToolRecall Client (daemon-first, fallback to SQLite).

The client module wraps daemon communication with a fallback:
  - If daemon is available: sends request via TransportClient (UDS)
  - If daemon is unavailable (error: "daemon_unavailable"): falls back to direct SQLite

IMPORTANT: These tests MUST carefully isolate from the real daemon running on
the system (at /run/user/1004/toolrecall.sock). We do this by:
  (1) Saving/restoring the _client singleton
  (2) Patching toolrecall.transport.DEFAULT_PATH to our temp socket
  (3) Using set_socket_path() so client code picks up the change

Tests cover:
  - _get_client() creates shared TransportClient singleton
  - _check_daemon() returns True/False
  - daemon_running() public API
  - set_socket_path() forces reconnect
  - mcp_call() builds correct payload, routes through daemon
  - mcp_list_servers() returns server list
  - All cached_* / docs_* functions use daemon-first pattern
  - Fallback to direct SQLite when daemon unavailable
"""

import json
import os
import socket
import sys
import threading
import time
import unittest
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


# ── Helpers ──────────────────────────────────


def _patch_transport(path):
    """Patch DEFAULT_PATH in ALL modules that import it, and reset singleton.

    Uses set_socket_path() which properly updates the transport module +
    client module internal state + forces a reconnect.
    """
    import toolrecall.client as cl
    cl.set_socket_path(path)
    import toolrecall.transport as tp
    tp.DEFAULT_PATH = path  # mb reads from transport module directly


def _fresh_client_module():
    """Force re-import of client module (for tests that modify singleon)."""
    import toolrecall.client as cl
    cl._client = None


class MockDaemon:
    """A minimal UDS server responding like the real ToolRecall daemon.

    Responds with flat dicts (not wrapped in {"result": ...})
    matching what the actual daemon returns for each cmd.
    """

    def __init__(self, socket_path: str):
        self.path = socket_path
        self.server = None
        self.received = []

    def start(self):
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            os.unlink(self.path)
        except FileNotFoundError:
            pass
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self.server.bind(self.path)
        self.server.listen(5)
        threading.Thread(target=self._serve, daemon=True).start()
        time.sleep(0.15)

    def _serve(self):
        while True:
            try:
                conn, _ = self.server.accept()
                raw = b""
                while True:
                    chunk = conn.recv(65536)
                    if not chunk:
                        break
                    raw += chunk
                    if len(raw) >= 4:
                        msg_len = int.from_bytes(raw[:4], "big")
                        if len(raw) >= 4 + msg_len:
                            break
                if len(raw) < 4:
                    conn.close()
                    continue
                msg_len = int.from_bytes(raw[:4], "big")
                payload = json.loads(raw[4:4+msg_len].decode("utf-8"))
                self.received.append(payload)
                cmd = payload.get("cmd", "")
                # Response matching actual daemon format
                responses = {
                    "cached_read": {"content": "daemon content", "cached": True},
                    "cached_terminal": {"output": "daemon shell output", "cached": True},
                    "cached_skill": {"content": "daemon skill content", "cached": True},
                    "cached_write": {"path": payload.get("path"), "unchanged": False},
                    "cached_patch": {"path": payload.get("path"), "unchanged": True, "reason": "already_applied"},
                    "docs_search": {"result": "docs_search daemon result"},
                    "docs_get_page": {"result": "docs_get_page daemon result"},
                    "cache_status": {"result": "Cache status from daemon"},
                    "cache_invalidate": {"result": "Cache invalidated via daemon"},
                    "cache_refresh_file": {"content": "refreshed content", "cached": False},
                    "mcp_list_servers": {"result": [{"name": "github", "running": True}]},
                    "mcp_call": {"result": {"status": "ok", "data": [1, 2, 3]}},
                }
                resp = responses.get(cmd, {"error": "unknown_cmd"})
                resp_bytes = json.dumps(resp).encode("utf-8")
                conn.sendall(len(resp_bytes).to_bytes(4, "big") + resp_bytes)
                conn.close()
            except Exception:
                break

    def stop(self):
        try:
            os.unlink(self.path)
        except FileNotFoundError:
            pass


# ═══════════════════════════════════════════════════════════
# No-daemon tests (singleton, fallback detection)
# ═══════════════════════════════════════════════════════════

class TestClientNoDaemon(unittest.TestCase):
    """Behavior when no daemon is running — singleton, ping, path."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.sock_path = os.path.join(self.tmpdir, "no_daemon.sock")
        _patch_transport(self.sock_path)
        _fresh_client_module()

    def tearDown(self):
        import toolrecall.client as cl
        cl._client = None
        # Don't restore DEFAULT_PATH — the next test's setUp patches it

    def test_get_client_returns_singleton(self):
        """_get_client() returns same TransportClient on repeated calls."""
        from toolrecall.client import _get_client
        c1 = _get_client()
        c2 = _get_client()
        self.assertIs(c1, c2)

    def test_get_client_uses_correct_path(self):
        """_get_client().path matches our patched DEFAULT_PATH."""
        from toolrecall.client import _get_client
        client = _get_client()
        self.assertEqual(client.path, self.sock_path)

    def test_daemon_running_returns_false(self):
        """daemon_running() returns False when no daemon on socket."""
        from toolrecall.client import daemon_running
        self.assertFalse(daemon_running())

    def test_set_socket_path_resets_singleton(self):
        """set_socket_path() clears _client and changes DEFAULT_PATH."""
        import toolrecall.client as cl
        c1 = cl._get_client()
        new_path = os.path.join(self.tmpdir, "new.sock")
        cl.set_socket_path(new_path)
        self.assertIsNone(cl._client, "Singleton should be cleared")
        c2 = cl._get_client()
        self.assertIsNot(c1, c2, "Should be a new client")
        self.assertEqual(c2.path, new_path,
                         "New client should use the updated socket path")


# ═══════════════════════════════════════════════════════════
# Daemon-first tests (mock daemon running)
# ═══════════════════════════════════════════════════════════

class TestClientDaemonFirst(unittest.TestCase):
    """Functions route through daemon when it's available."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.sock_path = os.path.join(self.tmpdir, "daemon_test.sock")
        self.daemon = MockDaemon(self.sock_path)
        self.daemon.start()
        _patch_transport(self.sock_path)
        _fresh_client_module()

    def tearDown(self):
        self.daemon.stop()
        import toolrecall.client as cl
        cl._client = None
        # Don't restore — next setUp patches it

    def test_mcp_call_routed_via_daemon(self):
        """mcp_call sends cmd=mcp_call payload and returns daemon result."""
        from toolrecall.client import mcp_call
        result = mcp_call("github", "list_issues", {"owner": "whiskybeer"})
        self.assertIn("result", result)
        self.assertEqual(result["result"]["status"], "ok")

    def test_mcp_call_with_bypass(self):
        """mcp_call with bypass_cache=True includes ttl=0 in payload."""
        from toolrecall.client import mcp_call
        mcp_call("time", "get_time", {"timezone": "UTC"}, bypass_cache=True)
        sent = [r for r in self.daemon.received
                if r.get("cmd") == "mcp_call" and r.get("ttl") == 0]
        self.assertGreater(len(sent), 0, "ttl=0 must be in sent payload")

    def test_mcp_list_servers(self):
        """mcp_list_servers returns server list from daemon."""
        from toolrecall.client import mcp_list_servers
        result = mcp_list_servers()
        self.assertIn("result", result)
        self.assertEqual(result["result"][0]["name"], "github")

    def test_cached_read(self):
        from toolrecall.client import cached_read
        result = cached_read("/tmp/test.txt")
        self.assertEqual(result.get("content"), "daemon content")

    def test_cached_terminal(self):
        from toolrecall.client import cached_terminal
        result = cached_terminal("echo hello")
        self.assertEqual(result.get("output"), "daemon shell output")

    def test_cached_skill(self):
        from toolrecall.client import cached_skill
        result = cached_skill("test-skill")
        self.assertEqual(result.get("content"), "daemon skill content")

    def test_cached_write(self):
        from toolrecall.client import cached_write
        result = cached_write("/tmp/test.txt", "hello")
        self.assertFalse(result.get("unchanged", True))
        self.assertEqual(result.get("path"), "/tmp/test.txt")

    def test_cached_patch(self):
        from toolrecall.client import cached_patch
        result = cached_patch("/tmp/test.txt", "old", "new")
        self.assertTrue(result.get("unchanged", False))
        self.assertEqual(result.get("reason"), "already_applied")

    def test_docs_search(self):
        from toolrecall.client import docs_search
        result = docs_search("python", source="docs")
        self.assertIn("daemon", result)

    def test_docs_get_page(self):
        from toolrecall.client import docs_get_page
        result = docs_get_page("docs", "readme.md")
        self.assertIn("daemon", result)

    def test_cache_status(self):
        from toolrecall.client import cache_status
        result = cache_status()
        self.assertIn("daemon", result)

    def test_cache_invalidate(self):
        from toolrecall.client import cache_invalidate
        result = cache_invalidate()
        self.assertIn("daemon", result)

    def test_refresh_file(self):
        from toolrecall.client import refresh_file
        result = refresh_file("/tmp/test.txt")
        self.assertFalse(result.get("cached", True))


# ═══════════════════════════════════════════════════════════
# Fallback tests (no daemon — direct SQLite)
# ═══════════════════════════════════════════════════════════

class TestClientFallbackDirect(unittest.TestCase):
    """When daemon is unavailable, client falls back to direct SQLite.

    Fallback calls toolrecall.cache functions directly which use the
    configured cache DB (TOOLRECALL_CACHE_DB).
    """

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.sock_path = os.path.join(self.tmpdir, "no_daemon.sock")
        self.db_path = os.path.join(self.tmpdir, "fallback.db")
        self.test_file = os.path.join(self.tmpdir, "test.txt")
        with open(self.test_file, "w") as f:
            f.write("fallback content")

        # Isolate DB + point client to non-existent socket
        os.environ["TOOLRECALL_CACHE_DB"] = self.db_path
        os.environ.pop("TOOLRECALL_SCAN_DIRS", None)
        _patch_transport(self.sock_path)
        _fresh_client_module()
        from toolrecall.cache import _init
        _init()

    def tearDown(self):
        os.environ.pop("TOOLRECALL_CACHE_DB", None)
        import toolrecall.client as cl
        cl._client = None
        # Don't restore — next setUp patches it

    def test_cached_read_fallback(self):
        from toolrecall.client import cached_read
        result = cached_read(self.test_file)
        self.assertIn("content", result)
        self.assertIn("fallback content", result.get("content", ""))

    def test_cached_read_fallback_blocked_outside_allowlist(self):
        """Fallback cached_read must block paths outside allowed_paths."""
        os.environ["TOOLRECALL_MCP_ALLOWED_PATHS"] = self.tmpdir
        _fresh_client_module()
        from toolrecall.client import cached_read
        result = cached_read("/etc/hosts")
        self.assertIn("error", result)
        self.assertIn("access denied", result["error"])

    def test_cached_terminal_fallback(self):
        from toolrecall.client import cached_terminal
        result = cached_terminal("echo fallback_works")
        # SECURITY: fail-closed — terminal requires the daemon
        self.assertIn("error", result)
        self.assertIn("daemon_unavailable", result["error"])

    def test_cached_skill_fallback(self):
        from toolrecall.client import cached_skill
        result = cached_skill("nonexistent-skill")
        self.assertTrue("error" in result or "content" in result)

    def test_cached_write_fallback(self):
        from toolrecall.client import cached_write
        path = os.path.join(self.tmpdir, "written.txt")
        result = cached_write(path, "fallback content")
        self.assertIn("path", result)

    def test_cached_patch_fallback(self):
        from toolrecall.client import cached_patch
        path = os.path.join(self.tmpdir, "patch_target.txt")
        with open(path, "w") as f:
            f.write("old content")
        result = cached_patch(path, "old", "new")
        self.assertIn("path", result)
        with open(path) as f:
            self.assertIn("new content", f.read())

    def test_cache_status_fallback(self):
        from toolrecall.client import cache_status
        result = cache_status()
        self.assertIsInstance(result, str)
        self.assertIn("Cache Status", result)

    def test_cache_invalidate_fallback(self):
        from toolrecall.client import cache_invalidate
        result = cache_invalidate()
        self.assertIn("direct", result)

    def test_docs_search_fallback(self):
        from toolrecall.client import docs_search
        result = docs_search("python")
        self.assertIsInstance(result, str)

    def test_docs_get_page_fallback(self):
        from toolrecall.client import docs_get_page
        result = docs_get_page("nonexistent", "nope.md")
        self.assertIsInstance(result, str)

    def test_refresh_file_fallback(self):
        from toolrecall.client import refresh_file
        result = refresh_file(self.test_file)
        self.assertIn("content", result)
        self.assertIn("fallback content", result.get("content", ""))


if __name__ == "__main__":
    unittest.main()
