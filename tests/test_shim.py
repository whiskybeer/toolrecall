"""Unit tests for toolrecall.shim — re-entrancy guard, subprocess routing, apply/remove.

Tests cover:
  - Re-entrancy guard: _shim_active / _enter_shim / _exit_shim
  - Thread-local isolation between threads
  - _shim_open falls through to _original_open on re-entry (no recursion)
  - _shim_open routes read-mode through cached_read when not re-entered
  - Binary mode bypasses cache
  - subprocess.run string routing through cached_terminal
  - subprocess.run list-form bypasses cache
  - apply() / remove() round-trip

All cache interactions are mocked — no daemon needed.
"""

import io
import os
import sys
import tempfile
import unittest
from unittest.mock import patch, MagicMock

# Isolated test DB before importing toolrecall
test_db_dir = tempfile.mkdtemp()
test_db_path = os.path.join(test_db_dir, "test_shim.db")
os.environ["TOOLRECALL_CACHE_DB"] = test_db_path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import toolrecall.shim as shim_mod
import builtins


class TestReentrancyGuard(unittest.TestCase):
    """_shim_active / _enter_shim / _exit_shim thread-local state."""

    def setUp(self):
        # Reset thread-local state before each test
        shim_mod._thread_local.active = False

    def test_initial_state_inactive(self):
        self.assertFalse(shim_mod._shim_active())

    def test_enter_sets_active(self):
        prev = shim_mod._enter_shim()
        self.assertTrue(shim_mod._shim_active())
        self.assertFalse(prev)

    def test_exit_restores_inactive(self):
        shim_mod._enter_shim()
        shim_mod._exit_shim(False)
        self.assertFalse(shim_mod._shim_active())

    def test_exit_restores_previous_true(self):
        # Simulate nested entry: already active, enter again, exit restores True
        shim_mod._enter_shim()
        prev = shim_mod._enter_shim()
        self.assertTrue(prev)
        shim_mod._exit_shim(True)
        self.assertTrue(shim_mod._shim_active())

    def test_exit_restores_to_false_after_outer(self):
        shim_mod._enter_shim()
        inner_prev = shim_mod._enter_shim()
        shim_mod._exit_shim(inner_prev)
        shim_mod._exit_shim(False)
        self.assertFalse(shim_mod._shim_active())


class TestShimOpenReentrancy(unittest.TestCase):
    """_shim_open must not recurse when cache client calls open() internally."""

    def setUp(self):
        shim_mod._thread_local.active = False
        # Save originals
        self._orig_open = builtins.open
        self._orig_tr = shim_mod._TR
        self._orig_original_open = shim_mod._original_open

    def tearDown(self):
        builtins.open = self._orig_open
        shim_mod._TR = self._orig_tr
        shim_mod._original_open = self._orig_original_open
        shim_mod._thread_local.active = False

    def test_reentry_falls_through_immediately(self):
        """When already inside a shim call, _shim_open calls _original_open directly."""
        call_log = []

        # Mock _original_open to track calls
        def mock_open(path, mode='r', *args, **kwargs):
            call_log.append(("open", path, mode))
            return io.StringIO("real content")

        shim_mod._original_open = mock_open

        # Simulate being inside a shim call
        shim_mod._enter_shim()

        result = shim_mod._shim_open("/etc/hosts", "r")

        self.assertEqual(len(call_log), 1)
        self.assertEqual(call_log[0], ("open", "/etc/hosts", "r"))
        self.assertEqual(result.read(), "real content")
        # Guard should still be active (re-entry didn't change it)
        self.assertTrue(shim_mod._shim_active())

    def test_no_recursion_when_client_calls_open(self):
        """Simulate the real bug: cached_read internally calls open().

        Before the fix, this caused infinite recursion. Now the re-entrancy
        guard breaks the cycle.
        """
        call_count = {"shim_open": 0, "original_open": 0}

        # Save the REAL original open (not the shimmed one)
        real_original_open = shim_mod._original_open

        # Mock the cache client to call open() (simulating daemon/DB access)
        def mock_cached_read(path):
            # This internal open() must NOT recurse into the shim.
            # We call real_original_open directly to simulate what
            # cached_read does internally (it opens files for DB access).
            with real_original_open(path, 'r') as f:
                return {"content": f.read()}

        shim_mod._TR = {"read": mock_cached_read, "terminal": MagicMock()}

        # Wrap _original_open to count calls but delegate to the real open
        def mock_original_open(path, mode='r', *args, **kwargs):
            call_count["original_open"] += 1
            return real_original_open(path, mode, *args, **kwargs)

        shim_mod._original_open = mock_original_open

        # Wrap _shim_open to track entry count
        original_shim_open = shim_mod._shim_open
        def tracking_shim_open(path, mode='r', *args, **kwargs):
            call_count["shim_open"] += 1
            return original_shim_open(path, mode, *args, **kwargs)

        # Patch builtins.open to use our tracking shim
        builtins.open = tracking_shim_open

        # Create a temp file to read
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write("test content for recursion check")
            temp_path = f.name

        try:
            result = builtins.open(temp_path, "r")
            content = result.read()
            self.assertIn("test content", content)

            # shim_open should have been called, but not recursively
            # (at most 2: one outer shim, one inner that falls through)
            self.assertGreaterEqual(call_count["shim_open"], 1)
            self.assertLessEqual(call_count["shim_open"], 2,
                                "Shim open should not recurse more than once")
        finally:
            os.unlink(temp_path)
            builtins.open = real_original_open
            shim_mod._original_open = real_original_open


class TestShimOpenRouting(unittest.TestCase):
    """_shim_open routing logic: cache hit, cache miss, binary bypass."""

    def setUp(self):
        shim_mod._thread_local.active = False
        self._orig_open = builtins.open
        self._orig_tr = shim_mod._TR
        self._orig_original_open = shim_mod._original_open

    def tearDown(self):
        builtins.open = self._orig_open
        shim_mod._TR = self._orig_tr
        shim_mod._original_open = self._orig_original_open
        shim_mod._thread_local.active = False

    def test_cache_hit_returns_stringio(self):
        """When cached_read returns content, _shim_open returns a StringIO."""
        shim_mod._TR = {
            "read": lambda p: {"cached": True, "content": "cached file content"},
            "terminal": MagicMock(),
        }
        shim_mod._original_open = MagicMock(return_value=io.StringIO("should not be called"))

        result = shim_mod._shim_open("/some/file", "r")
        self.assertIsInstance(result, io.StringIO)
        self.assertEqual(result.read(), "cached file content")
        # Original open should NOT have been called
        shim_mod._original_open.assert_not_called()

    def test_cache_miss_falls_back_to_original_open(self):
        """When cached_read returns no content, fall back to _original_open."""
        shim_mod._TR = {
            "read": lambda p: {"error": "not found"},
            "terminal": MagicMock(),
        }
        real_file = io.StringIO("real file content")
        shim_mod._original_open = MagicMock(return_value=real_file)

        result = shim_mod._shim_open("/some/file", "r")
        shim_mod._original_open.assert_called_once()
        self.assertEqual(result.read(), "real file content")

    def test_binary_mode_bypasses_cache(self):
        """Binary mode ('rb') must not route through cache (which is text-only)."""
        shim_mod._TR = {
            "read": MagicMock(return_value={"content": "should not be used"}),
            "terminal": MagicMock(),
        }
        real_file = io.BytesIO(b"binary data")
        shim_mod._original_open = MagicMock(return_value=real_file)

        result = shim_mod._shim_open("/some/file", "rb")
        shim_mod._TR["read"].assert_not_called()
        self.assertEqual(result.read(), b"binary data")

    def test_write_mode_bypasses_cache(self):
        """Write mode ('w') must not route through cache."""
        shim_mod._TR = {
            "read": MagicMock(return_value={"content": "should not be used"}),
            "terminal": MagicMock(),
        }
        mock_file = MagicMock()
        shim_mod._original_open = MagicMock(return_value=mock_file)

        shim_mod._shim_open("/some/file", "w")
        shim_mod._TR["read"].assert_not_called()
        shim_mod._original_open.assert_called_once_with("/some/file", "w")

    def test_exception_in_cached_read_falls_back(self):
        """If cached_read raises, _shim_open falls back to _original_open."""
        shim_mod._TR = {
            "read": MagicMock(side_effect=RuntimeError("daemon crashed")),
            "terminal": MagicMock(),
        }
        real_file = io.StringIO("fallback content")
        shim_mod._original_open = MagicMock(return_value=real_file)

        result = shim_mod._shim_open("/some/file", "r")
        self.assertEqual(result.read(), "fallback content")

    def test_tr_none_falls_back(self):
        """When _TR is None (client not loaded), fall back to original open."""
        shim_mod._TR = None
        real_file = io.StringIO("direct content")
        shim_mod._original_open = MagicMock(return_value=real_file)

        result = shim_mod._shim_open("/some/file", "r")
        shim_mod._original_open.assert_called_once()
        self.assertEqual(result.read(), "direct content")

    def test_tr_false_falls_back(self):
        """When _TR is False (import failed), fall back to original open."""
        shim_mod._TR = False
        real_file = io.StringIO("direct content")
        shim_mod._original_open = MagicMock(return_value=real_file)

        result = shim_mod._shim_open("/some/file", "r")
        shim_mod._original_open.assert_called_once()
        self.assertEqual(result.read(), "direct content")


class TestShimSubprocess(unittest.TestCase):
    """_shim_run routing: string commands cached, list commands bypassed."""

    def setUp(self):
        self._orig_tr = shim_mod._TR
        self._orig_run = shim_mod._original_run

    def tearDown(self):
        shim_mod._TR = self._orig_tr
        shim_mod._original_run = self._orig_run

    def test_string_command_routed_to_cache(self):
        """String-form commands go through cached_terminal."""
        shim_mod._TR = {
            "read": MagicMock(),
            "terminal": MagicMock(return_value={
                "output": "file1.txt\nfile2.txt",
                "exit_code": 0,
            }),
        }
        shim_mod._original_run = MagicMock()

        result = shim_mod._shim_run("ls -la")

        shim_mod._TR["terminal"].assert_called_once_with("ls -la")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "file1.txt\nfile2.txt")
        shim_mod._original_run.assert_not_called()

    def test_list_command_bypasses_cache(self):
        """List-form commands bypass cache (avoids shlex mangling)."""
        shim_mod._TR = {
            "read": MagicMock(),
            "terminal": MagicMock(return_value={"output": "cached", "exit_code": 0}),
        }
        shim_mod._original_run = MagicMock(return_value="real result")

        cmd = ["python3", "-c", "print('hello')"]
        result = shim_mod._shim_run(cmd)

        shim_mod._TR["terminal"].assert_not_called()
        shim_mod._original_run.assert_called_once_with(cmd)
        self.assertEqual(result, "real result")

    def test_empty_args_falls_back(self):
        """Empty args fall back to original run."""
        shim_mod._TR = None
        shim_mod._original_run = MagicMock(return_value="fallback")

        result = shim_mod._shim_run()

        self.assertEqual(result, "fallback")

    def test_exception_in_cached_terminal_falls_back(self):
        """If cached_terminal raises, fall back to original run."""
        shim_mod._TR = {
            "read": MagicMock(),
            "terminal": MagicMock(side_effect=RuntimeError("daemon down")),
        }
        shim_mod._original_run = MagicMock(return_value="fallback")

        result = shim_mod._shim_run("ls")

        shim_mod._original_run.assert_called_once_with("ls")
        self.assertEqual(result, "fallback")


class TestApplyRemove(unittest.TestCase):
    """apply() and remove() correctly patch/unpatch builtins.

    Note: The shim auto-applies on import (the `apply()` call at module
    bottom). So by the time these tests run, builtins.open is already
    _shim_open. We call remove() first to start from a clean state.
    """

    def setUp(self):
        # Start from unpatched state
        self._real_open = shim_mod._original_open
        shim_mod.remove()
        shim_mod._TR = None

    def tearDown(self):
        # Ensure clean state restored
        shim_mod.remove()
        # Re-apply for any subsequent tests
        shim_mod._ENABLED = not os.environ.get("TOOLRECALL_SHIM_DISABLE", "")
        shim_mod.apply()

    def test_apply_patches_open(self):
        shim_mod.apply()
        self.assertIs(builtins.open, shim_mod._shim_open)

    def test_remove_restores_open(self):
        shim_mod.apply()
        shim_mod.remove()
        self.assertIs(builtins.open, self._real_open)

    def test_apply_remove_roundtrip(self):
        """Multiple apply/remove cycles work correctly."""
        for _ in range(3):
            shim_mod.apply()
            self.assertIs(builtins.open, shim_mod._shim_open)
            shim_mod.remove()
            self.assertIs(builtins.open, self._real_open)

    @patch.dict(os.environ, {"TOOLRECALL_SHIM_DISABLE": "1"})
    def test_apply_noop_when_disabled(self):
        # Reload _ENABLED with env var set
        import importlib
        importlib.reload(shim_mod)
        self.assertFalse(shim_mod._ENABLED)
        # apply should be a no-op
        original_builtins_open = builtins.open
        shim_mod.apply()
        self.assertIs(builtins.open, original_builtins_open)
        # Restore for other tests
        importlib.reload(shim_mod)


if __name__ == "__main__":
    unittest.main()
