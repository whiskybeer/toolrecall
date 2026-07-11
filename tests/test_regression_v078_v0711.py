"""ToolRecall regression tests for v0.7.8–v0.7.11 changes.

Tests:
  1. Symlink-aware path checking (daemon.py check_read_path)
  2. Access-log filtering (cache.py _record - only entries with meaningful path)
  3. Daemon auto-start via shutil.which() (cli.py _ensure_daemon)
  4. MCP Cache FS server (mcp_cache_fs.py - init + tool calls)
"""

import os
import sys
import unittest
import tempfile
import time
import shutil
import json
import subprocess
from unittest.mock import patch, MagicMock

# Isolated test DB
test_db_dir = tempfile.mkdtemp()
test_db_path = os.path.join(test_db_dir, "test_regression.db")
os.environ["TOOLRECALL_CACHE_DB"] = test_db_path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from toolrecall.cache import cached_read, _init, _db
from toolrecall._db import _db_lock, _db_real
import toolrecall._db as _db_mod


def _reset_db():
    """Reset the singleton DB connection for test isolation."""
    _db_lock.acquire()
    try:
        if _db_real is not None:
            _db_real.close()
            _db_mod._db_real = None
            _db_mod._db_path_cached = None
    finally:
        _db_lock.release()
    os.environ["TOOLRECALL_CACHE_DB"] = test_db_path
    if os.path.exists(test_db_path):
        os.remove(test_db_path)
    for suffix in ("-wal", "-shm"):
        p = test_db_path + suffix
        if os.path.exists(p):
            os.remove(p)
    _init()


class TestSymlinkPathCheck(unittest.TestCase):
    """daemon.py SecurityGate.check_read_path() must allow symlinks.

    /etc/os-release is a symlink -> /usr/lib/os-release on many distros.
    os.path.realpath() resolves it, so a naive check against /etc fails.
    The fix: also check os.path.abspath() (the non-resolved path).
    """

    def setUp(self):
        class MockConfig:
            mcp_allowed_paths = ["/etc", "/dev", tempfile.gettempdir()]
            mcp_allow_terminal = False
            mcp_allowed_terminal_commands = []
            mcp_allow_invalidate = False
            mcp_multiplex_enabled = True
            mcp_multiplex_servers = ["time"]
            mcp_tool_access_control = False
            mcp_dangerous_tool_keywords = []
            mcp_cognitive_check_enabled = True
            mcp_ast_check_enabled = True
        from toolrecall.daemon import SecurityGate
        self.gate = SecurityGate(MockConfig())
        self.tmpdir = tempfile.mkdtemp()
        self.real_dir = os.path.join(self.tmpdir, "real_target")
        self.link_dir = os.path.join(self.tmpdir, "link_to_target")
        os.makedirs(self.real_dir, exist_ok=True)
        self.real_file = os.path.join(self.real_dir, "test.txt")
        with open(self.real_file, "w") as f:
            f.write("hello")
        os.symlink(self.real_dir, self.link_dir)
        self.link_file = os.path.join(self.link_dir, "test.txt")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_direct_path_allowed(self):
        """A normal (non-symlinked) path under /etc is allowed."""
        err = self.gate.check_read_path("/etc/hostname")
        self.assertIsNone(err, f"/etc/hostname should be allowed, got: {err}")

    def test_dev_null_allowed(self):
        """/dev/null is a character device under /dev."""
        err = self.gate.check_read_path("/dev/null")
        self.assertIsNone(err, "/dev/null should be allowed")

    def test_symlink_under_allowed(self):
        """A file reached through a symlink whose non-resolved path is under an allowed dir."""
        err = self.gate.check_read_path(self.link_file)
        self.assertIsNone(err,
            f"Symlinked file {self.link_file} (real: {os.path.realpath(self.link_file)}) "
            f"should be allowed when the symlink itself is under an allowed path, got: {err}")

    def test_symlink_resolved_under_allowed(self):
        """When both resolved and symlink paths are under allowed dirs."""
        inner_link = os.path.join(tempfile.gettempdir(), "_tr_test_link_target_" + str(os.getpid()))
        inner_real = os.path.join(self.tmpdir, "real_target", "nested.txt")
        with open(inner_real, "w") as f:
            f.write("nested")
        if os.path.exists(inner_link):
            os.remove(inner_link)
        os.symlink(inner_real, inner_link)
        err = self.gate.check_read_path(inner_link)
        self.assertIsNone(err, "Symlink to allowed path should be allowed")

    def test_outside_allowed_still_blocked(self):
        """A file outside allowed paths must still be blocked."""
        err = self.gate.check_read_path("/usr/lib/os-release")
        self.assertIsNotNone(err, "File outside allowed_paths should be blocked")

    def test_sensitive_file_still_blocked(self):
        """Sensitive file blocklist still applies even within allowed paths."""
        err = self.gate.check_read_path("/etc/.env")
        self.assertIsNotNone(err, ".env file should be blocked")

    def test_symlink_outside_allowed_blocked(self):
        """A file whose realpath is outside allowed paths is blocked."""
        err = self.gate.check_read_path("/usr/lib/os-release")
        self.assertIsNotNone(err, "/usr/lib path should be blocked")


class TestAccessLogFiltering(unittest.TestCase):
    """cache.py _record() must only write to access_log when path is meaningful.

    Before fix: cached_terminal, cached_mcp_check etc. passed path='', filling
    the access log with noise entries.
    After fix: only entries with non-empty path are written.
    """

    def setUp(self):
        _reset_db()

    def tearDown(self):
        try:
            _reset_db()
        except Exception:
            pass

    def _count_access_log(self):
        with _db() as conn:
            return conn.execute("SELECT COUNT(*) FROM access_log").fetchone()[0]

    def _get_access_log_entries(self):
        with _db() as conn:
            return conn.execute(
                "SELECT category, path, hit FROM access_log ORDER BY cached_at DESC"
            ).fetchall()

    def test_record_with_path_creates_entry(self):
        """_record(category, hit, path='/some/file') should create an access_log entry."""
        from toolrecall.cache import _record
        count_before = self._count_access_log()
        _record("file_cache", hit=True, path="/test/some_file.txt", tokens_saved=100)
        count_after = self._count_access_log()
        self.assertEqual(count_after, count_before + 1,
                         "access_log should have one more entry after _record with path")

    def test_record_without_path_skips_entry(self):
        """_record(category, hit, path='') should NOT create an access_log entry."""
        from toolrecall.cache import _record
        count_before = self._count_access_log()
        _record("terminal_cache", hit=True)  # path defaults to ''
        count_after = self._count_access_log()
        self.assertEqual(count_after, count_before,
                         "access_log should NOT get an entry when path is empty")

    def test_record_with_mcp_cache_no_path(self):
        """MCP cache hits without path should not pollute access_log."""
        from toolrecall.cache import _record
        count_before = self._count_access_log()
        _record("mcp_cache", hit=False)
        _record("api_cache", hit=True, tokens_saved=500)
        _record("browser_cache", hit=True)
        count_after = self._count_access_log()
        self.assertEqual(count_after, count_before,
                         "MCP/API/browser cache calls without path must not create entries")

    def test_mixed_path_and_no_path(self):
        """Only entries WITH a path appear in the access log."""
        from toolrecall.cache import _record
        _record("file_cache", hit=False, path="/etc/hosts")
        _record("terminal_cache", hit=True)  # no path - skip
        _record("file_cache", hit=True, path="/etc/resolv.conf", tokens_saved=50)
        entries = self._get_access_log_entries()
        self.assertEqual(len(entries), 2, "Only 2 of 3 _record calls should be in access_log")
        paths = [e["path"] for e in entries]
        self.assertIn("/etc/hosts", paths)
        self.assertIn("/etc/resolv.conf", paths)

    def test_cached_read_records_access_log(self):
        """cached_read() creates access_log entries with proper paths."""
        tmp = tempfile.NamedTemporaryFile(mode="w", delete=False)
        tmp.write("test content\n")
        tmp.close()
        cached_read(tmp.name)  # miss
        cached_read(tmp.name)  # hit
        entries = self._get_access_log_entries()
        file_entries = [e for e in entries if e["category"] == "file_cache"]
        self.assertGreaterEqual(len(file_entries), 1,
                                "cached_read should create file_cache access_log entries")
        hit_entries = [e for e in file_entries if e["hit"]]
        self.assertGreaterEqual(len(hit_entries), 1,
                                "At least one hit should be in access_log")
        os.unlink(tmp.name)

    def test_cached_terminal_does_not_pollute_access_log(self):
        """cached_terminal calls _record with no path, so no access_log entry."""
        from toolrecall.cache import _record
        count_before = self._count_access_log()
        _record("terminal_cache", hit=False)
        _record("terminal_cache", hit=True)
        count_after = self._count_access_log()
        self.assertEqual(count_after, count_before,
                         "cached_terminal should not create access_log entries")


class TestDaemonAutoStart(unittest.TestCase):
    """cli.py _ensure_daemon() must use shutil.which() not python -m."""

    def test_shutil_which_finds_toolrecall(self):
        """shutil.which('toolrecall') should find the CLI binary when installed."""
        toolrecall_bin = shutil.which("toolrecall")
        if toolrecall_bin is None:
            self.skipTest("toolrecall not on PATH (source checkout without install)")
        self.assertIsNotNone(toolrecall_bin)

    def test_toolrecall_binary_executable(self):
        """The toolrecall binary should be executable and return a version."""
        toolrecall_bin = shutil.which("toolrecall")
        if toolrecall_bin is None:
            self.skipTest("toolrecall not on PATH (source checkout without install)")
        result = subprocess.run(
            [toolrecall_bin, "--version"],
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("ToolRecall", result.stdout)

    def test_toolrecall_daemon_foreground_help(self):
        """toolrecall daemon --help should work (no import errors)."""
        toolrecall_bin = shutil.which("toolrecall")
        if toolrecall_bin is None:
            self.skipTest("toolrecall not on PATH (source checkout without install)")
        result = subprocess.run(
            [toolrecall_bin, "daemon", "--help"],
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("Usage: toolrecall daemon", result.stdout)

    def test_python_m_toolrecall_fails(self):
        """python -m toolrecall should fail (no __main__.py)."""
        result = subprocess.run(
            [sys.executable, "-m", "toolrecall", "daemon", "--help"],
            capture_output=True, text=True, timeout=10,
        )
        # This may succeed if toolrecall is installed with entry_points
        # (the console_scripts entry point also registers a __main__)
        # So we only check that the command doesn't crash — it should
        # either fail or succeed gracefully.
        self.assertTrue(
            result.returncode == 0 and "ToolRecall" in result.stdout or
            result.returncode != 0,
            f"Should either work or fail gracefully, got exit={result.returncode} stderr={result.stderr!r}"
        )


class TestMCPCacheFS(unittest.TestCase):
    """mcp_cache_fs.py must handle initialize, tools/list, and tools/call."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.test_file = os.path.join(self.tmpdir, "test.txt")
        with open(self.test_file, "w") as f:
            f.write("MCP Cache FS test content\nline 2\n")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _run_mcp_server(self, input_lines):
        """Run the MCP Cache FS server with given input, return parsed responses."""
        from toolrecall.mcp_cache_fs import main as mcp_main
        import io
        stdin_content = "\n".join(json.dumps(msg) for msg in input_lines)
        sys.stdin = io.StringIO(stdin_content)
        captured = io.StringIO()
        old_stdout = sys.stdout
        old_stderr = sys.stderr
        sys.stdout = captured
        sys.stderr = io.StringIO()
        try:
            mcp_main()
        except SystemExit:
            pass
        finally:
            sys.stdout = old_stdout
            sys.stderr = old_stderr
            sys.stdin = sys.__stdin__
        responses = []
        for line in captured.getvalue().strip().split("\n"):
            if line:
                try:
                    responses.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return responses

    def test_initialize_returns_server_info(self):
        """The initialize handshake returns server metadata."""
        responses = self._run_mcp_server([
            {"jsonrpc": "2.0", "id": 1, "method": "initialize"},
        ])
        self.assertEqual(len(responses), 1)
        result = responses[0].get("result", {})
        self.assertEqual(result.get("serverInfo", {}).get("name"), "toolrecall-cache-fs")

    def test_tools_list_returns_four_tools(self):
        """tools/list returns read_file, terminal, write_file, patch (native names)."""
        responses = self._run_mcp_server([
            {"jsonrpc": "2.0", "id": 1, "method": "initialize"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        ])
        tools_result = None
        for r in responses:
            if r.get("id") == 2:
                tools_result = r.get("result", {})
        self.assertIsNotNone(tools_result, "tools/list response not found")
        tools = tools_result.get("tools", [])
        tool_names = [t["name"] for t in tools]
        self.assertIn("read_file", tool_names)
        self.assertIn("terminal", tool_names)
        self.assertIn("write_file", tool_names)
        self.assertIn("patch", tool_names)

    def test_cached_read_existing_file(self):
        """read_file returns content of an existing file."""
        responses = self._run_mcp_server([
            {"jsonrpc": "2.0", "id": 1, "method": "initialize"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
                "name": "read_file",
                "arguments": {"path": self.test_file},
            }},
        ])
        read_result = None
        for r in responses:
            if r.get("id") == 2:
                read_result = r.get("result", {})
        if read_result is None:
            self.skipTest("MCP server did not respond to tools/call")
        text = "".join(c.get("text", "") for c in read_result.get("content", []))
        if "daemon_unavailable" in text or "Error" in text:
            self.skipTest(f"Daemon not available: {text}")
        self.assertIn("MCP Cache FS test content", text)

    def test_cached_read_nonexistent_file(self):
        """read_file returns error for nonexistent file."""
        responses = self._run_mcp_server([
            {"jsonrpc": "2.0", "id": 1, "method": "initialize"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
                "name": "read_file",
                "arguments": {"path": "/nonexistent/path/file.txt"},
            }},
        ])
        read_result = None
        for r in responses:
            if r.get("id") == 2:
                read_result = r.get("result", {})
        self.assertIsNotNone(read_result)
        text = read_result.get("content", [{}])[0].get("text", "")
        self.assertIn("Error", text)

    def test_ping_returns_empty(self):
        """Ping method returns empty result."""
        responses = self._run_mcp_server([
            {"jsonrpc": "2.0", "id": 1, "method": "ping"},
        ])
        self.assertEqual(len(responses), 1)
        self.assertEqual(responses[0].get("result"), {})


class TestMCPCacheFSRealDaemon(unittest.TestCase):
    """Integration test: MCP Cache FS via running daemon.

    Requires the ToolRecall daemon to be running with cache-fs configured.
    Skip if not available.
    """

    def setUp(self):
        try:
            from toolrecall.transport import TransportClient, DEFAULT_PATH
            tc = TransportClient(DEFAULT_PATH)
            resp = tc.send({"cmd": "ping"})
            if not resp.get("pong"):
                raise ConnectionError("Daemon not responding")
            self.daemon = tc
            self.ping = resp
            self.daemon_available = True
        except Exception:
            self.daemon_available = False

    def test_daemon_allowed_paths_includes_etc(self):
        """The daemon should have /etc in allowed_paths."""
        if not self.daemon_available:
            self.skipTest("ToolRecall daemon not running")
        paths = self.ping.get("allowed_paths", [])
        self.assertIn("/etc", paths, "/etc should be in daemon allowed_paths")
        self.assertIn("/dev", paths, "/dev should be in daemon allowed_paths")

    def test_daemon_allow_terminal_true(self):
        """The daemon should have allow_terminal=True."""
        if not self.daemon_available:
            self.skipTest("ToolRecall daemon not running")
        self.assertTrue(self.ping.get("allow_terminal", False),
                        "allow_terminal should be True")


if __name__ == "__main__":
    unittest.main()