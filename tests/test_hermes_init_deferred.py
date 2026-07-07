"""Unit tests for _deferred_patch retry loop in toolrecall.hermes_init.

Tests cover:
  - _deferred_patch retries until tool appears in registry
  - _deferred_patch gives up after 10 attempts (3s) without crashing
  - _deferred_patch calls patcher_fn when tool is found
  - _deferred_patch handles ImportError from tools.registry gracefully
  - Patcher receives the registry entry object
  - Retry timing: approximately 300ms per attempt

All registry interactions are mocked via sys.modules injection — no real
Hermes needed.
"""

import os
import sys
import time
import types
import tempfile
import unittest
from unittest.mock import patch, MagicMock

# Isolated test DB
test_db_dir = tempfile.mkdtemp()
test_db_path = os.path.join(test_db_dir, "test_deferred.db")
os.environ["TOOLRECALL_CACHE_DB"] = test_db_path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _inject_registry(mock_registry):
    """Inject a fake tools.registry module into sys.modules.

    This makes `from tools.registry import registry` work in tests
    without a real Hermes installation.
    """
    mock_module = types.ModuleType("tools.registry")
    mock_module.registry = mock_registry
    sys.modules["tools.registry"] = mock_module


def _remove_registry():
    """Remove the fake tools.registry from sys.modules."""
    sys.modules.pop("tools.registry", None)


class TestDeferredPatch(unittest.TestCase):
    """_deferred_patch: retry-until-found logic for transparent monkey-patching."""

    def setUp(self):
        # Import hermes_init (suppresses banner output)
        import io
        old_stdout = sys.stdout
        sys.stdout = io.StringIO()
        try:
            import toolrecall.hermes_init as hi
            self.hi = hi
        finally:
            sys.stdout = old_stdout

    def tearDown(self):
        _remove_registry()

    def test_patches_immediately_if_tool_exists(self):
        """When registry already has the tool, _deferred_patch patches without retry."""
        mock_entry = MagicMock()
        mock_registry = MagicMock()
        mock_registry.get_entry.return_value = mock_entry
        _inject_registry(mock_registry)

        patcher_fn = MagicMock()

        with patch("time.sleep") as mock_sleep:
            self.hi._deferred_patch("read_file", patcher_fn)

        patcher_fn.assert_called_once_with(mock_entry)
        mock_sleep.assert_not_called()

    def test_retries_until_tool_appears(self):
        """Tool appears after 3 attempts — _deferred_patch retries and succeeds."""
        mock_entry = MagicMock()
        mock_registry = MagicMock()
        # First 2 calls return None, 3rd returns the entry
        mock_registry.get_entry.side_effect = [None, None, mock_entry]
        _inject_registry(mock_registry)

        patcher_fn = MagicMock()

        with patch("time.sleep"):  # No real delays
            self.hi._deferred_patch("read_file", patcher_fn)

        self.assertEqual(mock_registry.get_entry.call_count, 3)
        patcher_fn.assert_called_once_with(mock_entry)

    def test_gives_up_after_10_attempts(self):
        """After 10 failed attempts, _deferred_patch silently gives up."""
        mock_registry = MagicMock()
        mock_registry.get_entry.return_value = None  # Never found
        _inject_registry(mock_registry)

        patcher_fn = MagicMock()

        with patch("time.sleep"):  # No real delays
            self.hi._deferred_patch("read_file", patcher_fn)

        self.assertEqual(mock_registry.get_entry.call_count, 10)
        patcher_fn.assert_not_called()

    def test_gives_up_after_importerror(self):
        """If tools.registry is never importable, _deferred_patch gives up after 10 attempts."""
        # Don't inject registry — ImportError on every attempt
        _remove_registry()

        patcher_fn = MagicMock()

        with patch("time.sleep"):
            self.hi._deferred_patch("read_file", patcher_fn)

        patcher_fn.assert_not_called()

    def test_patcher_receives_entry_object(self):
        """The patcher function receives the actual registry entry object."""
        mock_entry = MagicMock()
        mock_entry.handler = "original_handler"
        mock_registry = MagicMock()
        mock_registry.get_entry.return_value = mock_entry
        _inject_registry(mock_registry)

        captured_entry = []

        def my_patcher(entry):
            captured_entry.append(entry)

        with patch("time.sleep"):
            self.hi._deferred_patch("terminal", my_patcher)

        self.assertEqual(len(captured_entry), 1)
        self.assertIs(captured_entry[0], mock_entry)

    def test_retry_timing_approximately_300ms(self):
        """Each retry sleeps ~300ms. Verify sleep is called with 0.3."""
        mock_registry = MagicMock()
        mock_registry.get_entry.return_value = None
        _inject_registry(mock_registry)

        with patch("time.sleep") as mock_sleep:
            self.hi._deferred_patch("read_file", MagicMock())

        # Should have slept 10 times (once per failed attempt)
        self.assertEqual(mock_sleep.call_count, 10)
        for call in mock_sleep.call_args_list:
            self.assertAlmostEqual(call.args[0] if call.args else call.kwargs.get("delay", 0), 0.3, places=2)

    def test_completes_without_hanging(self):
        """_deferred_patch completes in reasonable time even when registry never appears."""
        with patch("time.sleep"):  # Speed up
            start = time.monotonic()
            self.hi._deferred_patch("read_file", MagicMock())
            elapsed = time.monotonic() - start

        # With mocked sleep, should complete in under 1s
        self.assertLess(elapsed, 1.0, "_deferred_patch took too long with mocked sleep")


class TestDeferredPatchIntegration(unittest.TestCase):
    """Integration: _deferred_patch + actual patcher functions from hermes_init.

    Tests that the patcher functions (_patch_read_file, _patch_terminal, etc.)
    correctly wrap a registry entry's handler when called.
    """

    def setUp(self):
        import io
        old_stdout = sys.stdout
        sys.stdout = io.StringIO()
        try:
            import toolrecall.hermes_init as hi
            self.hi = hi
        finally:
            sys.stdout = old_stdout

    def test_patch_read_file_wraps_handler(self):
        """_patch_read_file replaces entry.handler with a wrapper."""
        mock_entry = MagicMock()
        original_handler = MagicMock(return_value="original result")
        mock_entry.handler = original_handler

        self.hi._patch_read_file(mock_entry)

        # Handler should now be a wrapper (not the original)
        self.assertIsNot(mock_entry.handler, original_handler)

        # Wrapper should call cached_read and return on success
        with patch.object(self.hi, 'cached_read', return_value={"content": "cached content"}):
            result = mock_entry.handler({"path": "/test/file"})
            self.assertEqual(result, {"content": "cached content"})

    def test_patch_read_file_falls_back_on_error(self):
        """Wrapper falls back to original handler when cached_read returns error."""
        mock_entry = MagicMock()
        original_handler = MagicMock(return_value="original result")
        mock_entry.handler = original_handler

        self.hi._patch_read_file(mock_entry)

        with patch.object(self.hi, 'cached_read', return_value={"error": "not found"}):
            result = mock_entry.handler({"path": "/test/file"})
            self.assertEqual(result, "original result")
            original_handler.assert_called_once()

    def test_patch_terminal_wraps_handler(self):
        """_patch_terminal replaces entry.handler with a cached wrapper."""
        mock_entry = MagicMock()
        original_handler = MagicMock(return_value="original terminal result")
        mock_entry.handler = original_handler

        self.hi._patch_terminal(mock_entry)

        self.assertIsNot(mock_entry.handler, original_handler)

        with patch.object(self.hi, 'cached_terminal', return_value={"output": "cached output", "exit_code": 0}):
            result = mock_entry.handler({"command": "ls"})
            self.assertEqual(result, {"output": "cached output", "exit_code": 0})

    def test_patch_write_file_wraps_handler(self):
        """_patch_write_file replaces entry.handler."""
        mock_entry = MagicMock()
        original_handler = MagicMock(return_value="written")
        mock_entry.handler = original_handler

        self.hi._patch_write_file(mock_entry)

        self.assertIsNot(mock_entry.handler, original_handler)

        # When cached_write reports unchanged, wrapper returns skip message
        with patch.object(self.hi, 'cached_write', return_value={"unchanged": True}):
            result = mock_entry.handler({"path": "/test", "content": "same"})
            self.assertIn("unchanged", result)


if __name__ == "__main__":
    unittest.main()
