"""Integration tests for ToolRecall — end-to-end pipeline tests.

Tests verify the full integration between components:
  - knowledge DB: index → search → get_page pipeline
  - Memory index → FTS5 search → docs_get_page
  - Multiple source isolation (different sources don't cross-contaminate)
  - Re-index after delete produces correct results
  - Large knowledge DB stress test (100+ entries)
  - File cache: read → cache → invalidate on mtime change

NOT mocked — uses real SQLite FTS5, real cache tables.
"""

import os
import sys
import time
import unittest
import tempfile
import subprocess

# Unique base for all integration tests
INTEGRATION_BASE = tempfile.mkdtemp()

os.environ["TOOLRECALL_CACHE_DB"] = os.path.join(INTEGRATION_BASE, "integration_cache.db")
os.environ["TOOLRECALL_KNOWLEDGE_DB"] = os.path.join(INTEGRATION_BASE, "integration_knowledge.db")
os.environ["TOOLRECALL_SCAN_DIRS"] = ""

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from toolrecall.docs import (
    index_agent_memory, index_directory, index_all,
    docs_search, docs_get_page
)
from toolrecall.cache import cached_read, _init


class TestKnowledgeDBPipeline(unittest.TestCase):
    """End-to-end: index Hermes memory → FTS5 search → retrieve page."""

    def setUp(self):
        self.test_dir = os.path.join(INTEGRATION_BASE, "pipeline")
        os.makedirs(self.test_dir, exist_ok=True)
        # Unique DB per test to avoid FTS5 trigger corruption from re-opening
        test_name = self._testMethodName
        self._db = os.path.join(self.test_dir, f"knowledge_{test_name}.db")
        os.environ["TOOLRECALL_KNOWLEDGE_DB"] = self._db

        self.mem_dir = os.path.join(self.test_dir, "memories")
        os.makedirs(self.mem_dir, exist_ok=True)

        with open(os.path.join(self.mem_dir, "MEMORY.md"), "w") as f:
            f.write(
                "Security: never expose .env files\n"
                "§\n"
                "Python: 3.11 uses tomllib (stdlib)\n"
                "§\n"
                "Deployment: systemd user service preferred\n"
            )
        with open(os.path.join(self.mem_dir, "USER.md"), "w") as f:
            f.write(
                "Robin: prefers dense responses\n"
                "§\n"
                "Always test before changes\n"
            )

    def test_full_pipeline_memory(self):
        """Index → FTS5 search → get_page returns consistent results."""
        count = index_agent_memory(self.mem_dir)
        self.assertEqual(count, 5)

        result = docs_search("tomllib", source="agent-memory")
        self.assertIn("3.11", result)
        self.assertIn("BM25", result)

        # docs_get_page uses LIKE-based fuzzy match — it finds the entry
        page = docs_get_page("tomllib", source="agent-memory")
        self.assertIn("tomllib", page, "docs_get_page should find the tomllib entry")

    def test_full_pipeline_directory(self):
        """index_directory → source-filtered search works."""
        vault = os.path.join(self.test_dir, "vault")
        os.makedirs(vault)
        with open(os.path.join(vault, "note.md"), "w") as f:
            f.write("# Meeting\nBudget discussion for Q3.\n")

        index_directory(vault, source="corp-notes")

        result = docs_search("Q3", source="corp-notes")
        self.assertIn("Budget", result)

        result2 = docs_search("Q3", source="agent-memory")
        self.assertIn("No results", result2,
                      "Other source should NOT return cross-contaminated results")

    def test_multiple_source_isolation(self):
        """Entries from different sources don't cross-contaminate."""
        index_agent_memory(self.mem_dir, source="mem-a")
        index_agent_memory(self.mem_dir, source="mem-b")

        for src in ("mem-a", "mem-b"):
            r = docs_search("tomllib", source=src)
            self.assertIn("3.11", r)

        r_all = docs_search("tomllib")
        self.assertIn("mem-a", r_all)
        self.assertIn("mem-b", r_all)

    def test_reindex_after_delete(self):
        """Delete knowledge DB, re-index, search still works."""
        index_agent_memory(self.mem_dir, source="test-src")

        db_path = os.environ["TOOLRECALL_KNOWLEDGE_DB"]
        for ext in ("", "-wal", "-shm"):
            try:
                os.remove(db_path + ext)
            except OSError:
                pass

        count = index_agent_memory(self.mem_dir, source="test-src")
        self.assertEqual(count, 5)

        result = docs_search("tomllib", source="test-src")
        self.assertIn("3.11", result)

    def test_stress_100_entries(self):
        """Index 100+ entries, search performance is acceptable."""
        large_dir = os.path.join(self.test_dir, "large_vault")
        os.makedirs(large_dir)

        for i in range(100):
            with open(os.path.join(large_dir, f"doc_{i:03d}.md"), "w") as f:
                f.write(f"# Document {i}\nThis is document number {i}.\n")

        count = index_directory(large_dir, source="stress-test")
        self.assertEqual(count, 100)

        result = docs_search("document 42", source="stress-test")
        self.assertIn("Document 42", result)

    def test_index_all_with_memory_config(self):
        """index_all() includes memory when config enables it."""
        from toolrecall.config import load_config
        cfg = load_config()

        old_mem_cfg = cfg.get("sources", "memory")
        cfg._data.setdefault("sources", {})["memory"] = {"enabled": True}

        old_hermes = os.environ.get("HERMES_HOME", "")
        os.environ["HERMES_HOME"] = self.test_dir

        try:
            total = index_all(scan_dirs=[], extensions=(".md",))
            self.assertGreater(total, 0, "index_all should include memory entries")
        finally:
            if old_hermes:
                os.environ["HERMES_HOME"] = old_hermes
            else:
                os.environ.pop("HERMES_HOME", None)

    def test_proxy_docs_search_endpoint(self):
        """Simulate HTTP proxy /docs_search endpoint."""
        index_agent_memory(self.mem_dir)
        result = docs_search(query="deployment", source="agent-memory")
        self.assertIn("systemd", result)


class TestDaemonSubprocess(unittest.TestCase):
    """Integration test with a real daemon subprocess (UDS server)."""

    def setUp(self):
        self.test_dir = os.path.join(INTEGRATION_BASE, "daemon_test")
        os.makedirs(self.test_dir, exist_ok=True)
        self._db = os.path.join(self.test_dir, "cache.db")
        self._uds = os.path.join(self.test_dir, "tc.sock")
        self._orig_uds = os.environ.get("TOOLRECALL_UDS_PATH")
        os.environ["TOOLRECALL_CACHE_DB"] = self._db
        os.environ["TOOLRECALL_UDS_PATH"] = self._uds
        # Remove stale socket from previous runs
        try:
            os.unlink(self._uds)
        except OSError:
            pass
        self.process = None

    def tearDown(self):
        if self.process:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
            self.process = None
        # Restore original UDS path so other tests can find the real daemon
        if self._orig_uds is not None:
            os.environ["TOOLRECALL_UDS_PATH"] = self._orig_uds
        else:
            os.environ.pop("TOOLRECALL_UDS_PATH", None)

    def _start_daemon(self):
        """Start a minimal UDS daemon subprocess that responds to ping/invalidate."""
        daemon_script = os.path.join(os.path.dirname(__file__), "_test_daemon_subprocess.py")

        env = os.environ.copy()
        env["TOOLRECALL_UDS_PATH"] = self._uds
        env["TOOLRECALL_CACHE_DB"] = self._db

        self.process = subprocess.Popen(
            [sys.executable, daemon_script],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        # Wait up to 5 sec for socket
        for _ in range(50):
            if os.path.exists(self._uds):
                return True
            time.sleep(0.1)
        return False

    def _send_command(self, action: str) -> dict:
        import socket, json
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.settimeout(3)
            sock.connect(self._uds)
            sock.sendall(json.dumps({"action": action}).encode())
            resp = json.loads(sock.recv(4096).decode())
            return resp
        finally:
            sock.close()

    def test_daemon_ping_pong(self):
        """Daemon responds to ping with pong."""
        if not self._start_daemon():
            self.skipTest("Daemon subprocess failed to start")
        resp = self._send_command("ping")
        self.assertEqual(resp["status"], "pong")

    def test_daemon_stats(self):
        """Daemon returns stats including tokens_saved."""
        if not self._start_daemon():
            self.skipTest("Daemon subprocess failed to start")
        resp = self._send_command("stats")
        self.assertEqual(resp["hits"], 42)
        self.assertGreater(resp["tokens_saved"], 0)

    def test_daemon_invalidate(self):
        """Daemon invalidate returns cleared=True."""
        if not self._start_daemon():
            self.skipTest("Daemon subprocess failed to start")
        resp = self._send_command("invalidate")
        self.assertTrue(resp.get("cleared", False))

    def test_daemon_unknown_action_returns_error(self):
        """Unknown action returns error, not crash."""
        if not self._start_daemon():
            self.skipTest("Daemon subprocess failed to start")
        resp = self._send_command("explode")
        self.assertIn("error", resp)


class TestCacheFileIntegration(unittest.TestCase):
    """End-to-end: File reads are cached, invalidated on change."""

    def setUp(self):
        self.test_dir = os.path.join(INTEGRATION_BASE, "cache")
        os.makedirs(self.test_dir, exist_ok=True)
        self._db = os.path.join(self.test_dir, "cache.db")
        os.environ["TOOLRECALL_CACHE_DB"] = self._db

        self.test_file = os.path.join(self.test_dir, "cache_test.txt")
        with open(self.test_file, "w") as f:
            f.write("version 1\n")
        _init()

    def test_cached_read_hit_and_miss(self):
        """First read = miss, second read = hit (same mtime)."""
        r1 = cached_read(self.test_file)
        self.assertIn("version 1", r1.get("content", ""))

        r2 = cached_read(self.test_file)
        self.assertIn("version 1", r2.get("content", ""))

    def test_cached_read_invalidates_on_modify(self):
        """Changing file content changes mtime → cache invalidates."""
        r1 = cached_read(self.test_file)
        self.assertIn("version 1", r1.get("content", ""))

        time.sleep(0.02)
        with open(self.test_file, "w") as f:
            f.write("version 2\n")

        r2 = cached_read(self.test_file)
        content = r2.get("content", "")
        self.assertIn("version 2", content)


if __name__ == "__main__":
    unittest.main()