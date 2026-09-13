# Copyright (c) 2026 Robin Schautka
# SPDX-License-Identifier: MIT
# Source: https://github.com/whiskybeer/toolrecall
"""Context-tracker pollution gate: only agent_tool reads are tracked.

Poison (measured live 2026-09-12): the Hermes gateway's shim-tagged infra
reads (models_dev_cache.json 1.15M tok, proxy_usage.csv 793K tok) landed in
the tracker's clean set because _handle_read called mark_read unconditionally
— 70% of the "ctx dropped" figure was infra data never in any LLM context.

Gate contract:
  - source="agent_tool"  → mark_read (tracked, droppable)
  - source="shim"/absent → NOT tracked
  - ~/.toolrecall/** reads are never tracked (default ignore prefix)
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from toolrecall.context_tracker import ContextTracker  # noqa: E402


class TestSourceGate(unittest.TestCase):
    """Unit: the tracker itself is source-agnostic; the GATE is in the daemon.
    These tests pin the daemon-side decision via the tracker's public API +
    the daemon handler logic (integration below)."""

    def setUp(self):
        self.tracker = ContextTracker()

    def test_agent_tool_read_is_tracked(self):
        with tempfile.NamedTemporaryFile(suffix=".py", delete=False) as f:
            f.write(b"x = 1\n")
            path = f.name
        self.addCleanup(os.unlink, path)
        self.tracker.mark_read(path)
        stats = self.tracker.get_stats()
        self.assertIn(path, stats["clean"])

    def test_tracker_has_no_source_gate_by_design(self):
        """The gate lives in the daemon (one choke point — every read passes
        _handle_read); the tracker stays a dumb ledger. This test documents
        that: mark_read takes no source arg."""
        import inspect

        sig = inspect.signature(ContextTracker.mark_read)
        self.assertEqual(list(sig.parameters), ["self", "path"])


class TestIgnorePrefixes(unittest.TestCase):
    def test_toolrecall_state_dir_ignored_by_default(self):
        """Config default includes ~/.toolrecall (property reads its own
        defaults; verify against the class default via the real property
        on a real Config instance built from env-free init)."""
        from toolrecall.config import Config

        cfg = Config()  # loads defaults + user TOML; property works regardless
        prefixes = cfg.tracker_ignore_read_prefixes
        self.assertTrue(
            any(p.rstrip("/").endswith(".toolrecall") for p in prefixes),
            f"~/.toolrecall must be a default ignore prefix, got: {prefixes}",
        )

    def test_tracker_respects_ignore_prefix(self):
        tracker = ContextTracker(ignore_prefixes=["~/.toolrecall/"])
        expanded = os.path.expanduser("~/.toolrecall/proxy_usage.csv")
        # Mark with the RAW (tilde) path — mark_read expands internally.
        tracker.mark_read("~/.toolrecall/proxy_usage.csv")
        self.assertNotIn(expanded, tracker.get_stats()["clean"])


class TestDaemonReadGate(unittest.TestCase):
    """Integration: _handle_read tracks agent_tool reads only.

    Drives the real handler method with a stubbed security gate and cache
    (no daemon process needed) — the gate decision is what's under test.
    """

    def _run_handle_read(self, source):
        import tempfile

        from unittest.mock import MagicMock, patch

        import toolrecall.daemon as daemon

        with tempfile.NamedTemporaryFile(suffix=".py", delete=False) as f:
            f.write(b"data = 1\n")
            path = f.name
        self.addCleanup(os.unlink, path)

        server = daemon.DaemonServer.__new__(daemon.DaemonServer)
        server.security = MagicMock()
        server.security.check_read_path_for.return_value = None
        server._context = ContextTracker()
        fake_read = {"cached": False, "content": "data = 1\n", "path": path}
        with (
            patch.object(daemon, "_cache_read", return_value=fake_read),
            patch.object(type(server), "_context", create=True, new_callable=lambda: None)
            if False
            else _noop(),
        ):
            # _handle_read calls self._context.mark_read — our tracker
            server._handle_read({"path": path, "source": source})
        return server._context, path

    def test_shim_tagged_read_not_tracked(self):
        tracker, path = self._run_handle_read("shim")
        self.assertNotIn(path, tracker.get_stats()["clean"])

    def test_absent_source_not_tracked(self):
        tracker, path = self._run_handle_read("")
        self.assertNotIn(path, tracker.get_stats()["clean"])

    def test_agent_tool_read_tracked(self):
        tracker, path = self._run_handle_read("agent_tool")
        self.assertIn(path, tracker.get_stats()["clean"])


class _noop:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


if __name__ == "__main__":
    unittest.main()
