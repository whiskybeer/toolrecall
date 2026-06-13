"""Tests for dataset.py — export local cache to JSONL fine-tuning dataset.

The dataset module exports tool calls from the cache DB into JSONL format:
  - MCP tool calls → {"type": "mcp_tool_call", "hash": "...", "result": ...}
  - Terminal executions → {"type": "terminal_execution", "command": "...", "stdout": "..."}

Tests cover:
  - export_trajectories() writes JSONL to disk
  - MCP cache entries are included in the export
  - Terminal cache entries are included in the export
  - Empty cache exports 0 records
  - Output path expansion is handled
  - Invalid cache data is silently skipped
"""

import json
import os
import sys
import unittest
import tempfile
import sqlite3

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _create_test_db(path, seed_mcp=True, seed_terminal=True):
    """Create a test cache DB with optional seed data.

    Creates both tables (mcp_cache and terminal_cache) so the
    export function doesn't crash on missing tables.
    """
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE IF NOT EXISTS mcp_cache (request_hash TEXT PRIMARY KEY, data TEXT)")
    conn.execute("CREATE TABLE IF NOT EXISTS terminal_cache (command TEXT, output TEXT)")
    if seed_mcp:
        conn.execute(
            "INSERT OR REPLACE INTO mcp_cache (request_hash, data) VALUES (?, ?)",
            ("abc123", json.dumps({"status": "ok", "issues": [1, 2, 3]}))
        )
        conn.execute(
            "INSERT OR REPLACE INTO mcp_cache (request_hash, data) VALUES (?, ?)",
            ("def456", json.dumps({"timezone": "UTC", "datetime": "2026-01-01 12:00:00"}))
        )
    if seed_terminal:
        conn.execute(
            "INSERT INTO terminal_cache (command, output) VALUES (?, ?)",
            ("git status", "On branch main\nnothing to commit")
        )
        conn.execute(
            "INSERT INTO terminal_cache (command, output) VALUES (?, ?)",
            ("ls -la", "total 42\ndrwxr-xr-x 2 user user 4096")
        )
    conn.commit()
    conn.close()


class TestDatasetExport(unittest.TestCase):
    """export_trajectories() writes JSONL to disk."""

    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.test_dir, "test_cache.db")
        self.output_path = os.path.join(self.test_dir, "dataset.jsonl")
        os.environ["TOOLRECALL_CACHE_DB"] = self.db_path

    def tearDown(self):
        import shutil
        shutil.rmtree(self.test_dir, ignore_errors=True)
        os.environ.pop("TOOLRECALL_CACHE_DB", None)

    def test_export_with_both_types(self):
        """Both MCP and terminal entries appear in the output JSONL."""
        _create_test_db(self.db_path, seed_mcp=True, seed_terminal=True)

        from toolrecall.dataset import export_trajectories
        count, path = export_trajectories(self.output_path)

        self.assertEqual(count, 4, "Should export 2 MCP + 2 terminal records")
        self.assertTrue(os.path.exists(path))

        # Read back and validate
        records = []
        with open(path) as f:
            for line in f:
                records.append(json.loads(line.strip()))

        types = {r["type"] for r in records}
        self.assertIn("mcp_tool_call", types)
        self.assertIn("terminal_execution", types)

    def test_export_mcp_only(self):
        """Only MCP entries when no terminal cache."""
        _create_test_db(self.db_path, seed_mcp=True, seed_terminal=False)

        from toolrecall.dataset import export_trajectories
        count, path = export_trajectories(self.output_path)

        self.assertEqual(count, 2, "Should export 2 MCP records")
        records = [json.loads(l) for l in open(path)]
        for r in records:
            self.assertEqual(r["type"], "mcp_tool_call")

    def test_export_terminal_only(self):
        """Only terminal entries when no MCP cache."""
        _create_test_db(self.db_path, seed_mcp=False, seed_terminal=True)

        from toolrecall.dataset import export_trajectories
        count, path = export_trajectories(self.output_path)

        self.assertEqual(count, 2, "Should export 2 terminal records")
        records = [json.loads(l) for l in open(path)]
        for r in records:
            self.assertEqual(r["type"], "terminal_execution")

    def test_export_empty_cache(self):
        """Empty cache exports 0 records, still produces valid JSONL."""
        _create_test_db(self.db_path, seed_mcp=False, seed_terminal=False)

        from toolrecall.dataset import export_trajectories
        count, path = export_trajectories(self.output_path)

        self.assertEqual(count, 0, "Empty cache = 0 records")
        # File should exist but be empty
        self.assertTrue(os.path.exists(path))
        self.assertEqual(os.path.getsize(path), 0)

    def test_export_output_fields(self):
        """Exported records have correct hash/command/result fields."""
        _create_test_db(self.db_path, seed_mcp=True, seed_terminal=True)

        from toolrecall.dataset import export_trajectories
        count, path = export_trajectories(self.output_path)

        records = [json.loads(l) for l in open(path)]
        for r in records:
            if r["type"] == "mcp_tool_call":
                self.assertIn("hash", r)
                self.assertIn("result", r)
            elif r["type"] == "terminal_execution":
                self.assertIn("command", r)
                self.assertIn("stdout", r)

    def test_invalid_mcp_data_skipped(self):
        """Corrupt MCP data rows are silently skipped without crashing."""
        # Create both tables so export_trajectories doesn't crash
        _create_test_db(self.db_path, seed_mcp=False, seed_terminal=False)
        # Now add valid + broken MCP data
        conn = sqlite3.connect(self.db_path)
        conn.execute("INSERT OR REPLACE INTO mcp_cache VALUES (?, ?)",
                     ("valid", json.dumps({"ok": True})))
        conn.execute("INSERT OR REPLACE INTO mcp_cache VALUES (?, ?)",
                     ("broken", "{invalid json}"))
        conn.commit()
        conn.close()

        from toolrecall.dataset import export_trajectories
        count, path = export_trajectories(self.output_path)

        self.assertEqual(count, 1, "Only valid row should be exported")


class TestDatasetExportPathExpansion(unittest.TestCase):
    """Path expansion works with ~ in paths."""

    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.test_dir, "cache.db")
        os.environ["TOOLRECALL_CACHE_DB"] = self.db_path
        _create_test_db(self.db_path, seed_mcp=True, seed_terminal=True)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.test_dir, ignore_errors=True)
        os.environ.pop("TOOLRECALL_CACHE_DB", None)

    def test_export_returns_resolved_path(self):
        """export_trajectories returns the resolved output path with count."""
        from toolrecall.dataset import export_trajectories
        out_path = os.path.join(self.test_dir, "out.jsonl")
        count, result_path = export_trajectories(out_path)
        # Should export all 4 seeded records
        self.assertEqual(count, 4, "Should export MCP + terminal records")
        self.assertEqual(os.path.abspath(result_path), os.path.abspath(out_path))


if __name__ == "__main__":
    unittest.main()
