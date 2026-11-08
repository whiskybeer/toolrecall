"""Unit tests for toolrecall.shim — re-entrancy guard, file-read routing, apply/remove.

Tests cover:
  - Re-entrancy guard: _shim_active / _enter_shim / _exit_shim
  - Thread-local isolation between threads
  - _shim_open falls through to _original_open on re-entry (no recursion)
  - _shim_open routes read-mode through cached_read when not re-entered
  - Binary / write modes bypass cache
  - apply() / remove() round-trip
  - Option B invariant: apply() does NOT patch subprocess.run/Popen

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
        def mock_open(path, mode="r", *args, **kwargs):
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
            with real_original_open(path, "r") as f:
                return {"content": f.read()}

        shim_mod._TR = {"read": mock_cached_read}

        # Wrap _original_open to count calls but delegate to the real open
        def mock_original_open(path, mode="r", *args, **kwargs):
            call_count["original_open"] += 1
            return real_original_open(path, mode, *args, **kwargs)

        shim_mod._original_open = mock_original_open

        # Wrap _shim_open to track entry count
        original_shim_open = shim_mod._shim_open

        def tracking_shim_open(path, mode="r", *args, **kwargs):
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
            # (at most 5: one outer shim, plus a few during _get_tr import
            # chain which loads config and client modules)
            self.assertGreaterEqual(call_count["shim_open"], 1)
            self.assertLessEqual(
                call_count["shim_open"], 5, "Shim open should not recurse excessively"
            )
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
        self._orig_skip_prefixes = shim_mod._SKIP_PREFIXES
        self._orig_enabled = shim_mod._ENABLED

    def tearDown(self):
        builtins.open = self._orig_open
        shim_mod._TR = self._orig_tr
        shim_mod._original_open = self._orig_original_open
        shim_mod._SKIP_PREFIXES = self._orig_skip_prefixes
        shim_mod._ENABLED = self._orig_enabled
        shim_mod._thread_local.active = False

    def test_cache_hit_returns_real_file(self):
        """When cached_read returns content, _shim_open returns a real file
        object (backed by a temp file), not a StringIO."""
        shim_mod._TR = {
            "read": lambda p, **kwargs: {"cached": True, "content": "cached file content"},
        }
        # _original_open (captured at import, before the shim patched
        # builtins.open) is used by the temp-file materializer; leave it as
        # the real open so the returned handle has full file semantics.

        result = shim_mod._shim_open("/some/file", "r")
        self.assertNotIsInstance(result, io.StringIO)
        self.assertEqual(result.read(), "cached file content")
        self.assertTrue(hasattr(result, "fileno"))
        result.close()

    def test_cache_miss_falls_back_to_original_open(self):
        """When cached_read returns no content, fall back to _original_open."""
        shim_mod._TR = {
            "read": lambda p, **kwargs: {"error": "not found"},
        }
        shim_mod._SKIP_PREFIXES = []  # prevent config-load calls
        real_file = io.StringIO("real file content")
        shim_mod._original_open = MagicMock(return_value=real_file)

        result = shim_mod._shim_open("/some/file", "r")
        shim_mod._original_open.assert_called_once()
        self.assertEqual(result.read(), "real file content")

    def test_binary_mode_bypasses_cache(self):
        """Binary mode ('rb') must not route through cache (which is text-only)."""
        shim_mod._TR = {
            "read": MagicMock(return_value={"content": "should not be used"}),
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
        }
        real_file = io.StringIO("fallback content")
        shim_mod._original_open = MagicMock(return_value=real_file)

        result = shim_mod._shim_open("/some/file", "r")
        self.assertEqual(result.read(), "fallback content")

    def test_tr_none_falls_back(self):
        """When _TR is None (client not loaded), fall back to original open."""
        shim_mod._TR = None
        shim_mod._ENABLED = False  # prevent lazy import (which triggers config reads)
        shim_mod._SKIP_PREFIXES = []  # prevent config-load calls in _should_skip
        real_file = io.StringIO("direct content")
        shim_mod._original_open = MagicMock(return_value=real_file)

        result = shim_mod._shim_open("/some/file", "r")
        shim_mod._original_open.assert_called_once()
        self.assertEqual(result.read(), "direct content")

    def test_tr_false_falls_back(self):
        """When _TR is False (import failed), fall back to original open."""
        shim_mod._TR = False
        shim_mod._SKIP_PREFIXES = []
        real_file = io.StringIO("direct content")
        shim_mod._original_open = MagicMock(return_value=real_file)

        result = shim_mod._shim_open("/some/file", "r")
        shim_mod._original_open.assert_called_once()
        self.assertEqual(result.read(), "direct content")

    # ─── R+/W+/A must not be intercepted (writes would go to StringIO) ───

    def test_read_write_mode_bypasses_cache(self):
        """'r+' mode must not be intercepted (writes would go to StringIO)."""
        shim_mod._TR = {
            "read": MagicMock(return_value={"cached": True, "content": "fake"}),
        }
        real_file = MagicMock()
        shim_mod._original_open = MagicMock(return_value=real_file)

        _ = shim_mod._shim_open("/some/file", "r+")
        shim_mod._TR["read"].assert_not_called()
        shim_mod._original_open.assert_called_once_with("/some/file", "r+")

    def test_write_plus_mode_bypasses_cache(self):
        """'w+' mode must not be intercepted."""
        shim_mod._TR = {
            "read": MagicMock(return_value={"cached": True, "content": "fake"}),
        }
        real_file = MagicMock()
        shim_mod._original_open = MagicMock(return_value=real_file)

        shim_mod._shim_open("/some/file", "w+")
        shim_mod._TR["read"].assert_not_called()

    def test_append_mode_bypasses_cache(self):
        """'a' mode must not be intercepted."""
        shim_mod._TR = {
            "read": MagicMock(return_value={"cached": True, "content": "fake"}),
        }
        real_file = MagicMock()
        shim_mod._original_open = MagicMock(return_value=real_file)

        shim_mod._shim_open("/some/file", "a")
        shim_mod._TR["read"].assert_not_called()

    def test_rt_mode_is_intercepted(self):
        """'rt' mode is a pure read and should be intercepted.

        The intercepted hit must still return a real file object (real
        fileno / buffer semantics), never a StringIO.
        """
        shim_mod._TR = {
            "read": MagicMock(return_value={"cached": True, "content": "cached rt content"}),
        }
        # _original_open (captured at import, before builtins.open was
        # patched to _shim_open) is the real open used by the materializer.

        result = shim_mod._shim_open("/some/file", "rt")
        self.assertNotIsInstance(result, io.StringIO)
        self.assertEqual(result.read(), "cached rt content")
        self.assertTrue(hasattr(result, "fileno"))
        result.close()


class TestOptionBNoSubprocess(unittest.TestCase):
    """Option B invariant: the shim must NOT intercept subprocess.

    The lossy terminal-caching shim was removed. terminal command output is
    never routed through the daemon at the shim layer, and apply() must leave
    subprocess.run/Popen untouched so commands run natively in the calling
    agent's cwd/env.
    """

    def test_subprocess_modules_are_not_defined_by_shim(self):
        """The shim no longer ships _shim_run/_shim_popen/_CachedPopen."""
        self.assertFalse(hasattr(shim_mod, "_shim_run"))
        self.assertFalse(hasattr(shim_mod, "_shim_popen"))
        self.assertFalse(hasattr(shim_mod, "_CachedPopen"))
        self.assertFalse(hasattr(shim_mod, "_is_safe_string_command"))

    def test_apply_does_not_patch_subprocess_popen(self):
        """apply() must leave subprocess.Popen untouched (run natively)."""
        import subprocess

        original_popen = subprocess.Popen
        shim_mod.apply(force=True)
        try:
            self.assertIs(
                subprocess.Popen, original_popen, "Option B: subprocess.Popen must NOT be shimmed"
            )
        finally:
            shim_mod.remove()

    def test_apply_does_not_patch_subprocess_run(self):
        """apply() must leave subprocess.run untouched."""
        import subprocess

        original_run = subprocess.run
        shim_mod.apply(force=True)
        try:
            self.assertIs(
                subprocess.run, original_run, "Option B: subprocess.run must NOT be shimmed"
            )
        finally:
            shim_mod.remove()


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
        # Temporarily bypass pytest detection so apply() actually patches
        self._orig_argv = sys.argv[:]
        sys.argv = ["python"]
        self._orig_env = os.environ.pop("PYTEST_CURRENT_TEST", None)
        # Temporarily re-enable the shim (conftest sets TOOLRECALL_SHIM_DISABLE=1)
        self._orig_enabled = shim_mod._ENABLED
        shim_mod._ENABLED = True

    def tearDown(self):
        # Ensure clean state restored
        sys.argv = self._orig_argv
        if self._orig_env is not None:
            os.environ["PYTEST_CURRENT_TEST"] = self._orig_env
        shim_mod._ENABLED = self._orig_enabled
        shim_mod.remove()
        # Re-apply for any subsequent tests (but not under pytest)
        shim_mod.apply(force=True)

    def test_apply_patches_open(self):
        shim_mod.apply(force=True)
        self.assertIs(builtins.open, shim_mod._shim_open)

    def test_remove_restores_open(self):
        shim_mod.apply(force=True)
        shim_mod.remove()
        self.assertIs(builtins.open, self._real_open)

    def test_apply_remove_roundtrip(self):
        """Multiple apply/remove cycles work correctly."""
        for _ in range(3):
            shim_mod.apply(force=True)
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
        shim_mod.apply(force=True)
        self.assertIs(builtins.open, original_builtins_open)
        # Restore for other tests
        importlib.reload(shim_mod)

    def test_apply_noop_when_marker_file_present(self):
        """Persistent marker file disables the shim at import (shim --disable).

        Runs a SUBPROCESS with a fresh interpreter so the marker env var is set
        BEFORE toolrecall.shim is imported — the import-time gate then sees the
        marker and must leave builtins.open unpatched. This is hermetic: no
        importlib.reload, so it cannot leak client/cache state into later tests.
        """
        import subprocess as _sp
        import sys as _sys

        with tempfile.TemporaryDirectory() as marker_dir:
            marker = os.path.join(marker_dir, "shim.disabled")
            with open(marker, "w") as f:
                f.write("disabled")
            code = (
                "import os, builtins; "
                "from toolrecall import shim as m; "
                "print('ENABLED', m._ENABLED); "
                "print('PATCHED', builtins.open is m._shim_open); "
                "print('DISABLED', m._marker_disabled())"
            )
            env = dict(os.environ)
            env["TOOLRECALL_SHIM_MARKER"] = marker
            r = _sp.run(
                [_sys.executable, "-c", code],
                capture_output=True,
                text=True,
                timeout=30,
                env=env,
            )
            self.assertEqual(r.returncode, 0, r.stderr)
            out = r.stdout
            self.assertIn("ENABLED False", out)
            self.assertIn("PATCHED False", out)
            self.assertIn("DISABLED True", out)

    def test_apply_noop_when_marker_absent_subprocess(self):
        """Without a marker, a fresh interpreter patches open() (sanity check)."""
        import subprocess as _sp
        import sys as _sys

        code = (
            "import builtins; "
            "from toolrecall import shim as m; "
            "print('PATCHED', builtins.open is m._shim_open)"
        )
        env = dict(os.environ)
        env.pop("TOOLRECALL_SHIM_DISABLE", None)
        env.pop("TOOLRECALL_SHIM_MARKER", None)
        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        env["PYTHONPATH"] = repo + os.pathsep + env.get("PYTHONPATH", "")
        r = _sp.run(
            [_sys.executable, "-c", code],
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
            cwd="/tmp",
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("PATCHED True", r.stdout)

    def test_disable_enable_roundtrip(self):
        """enable()/disable() write/clear the marker and flip the import gate.

        Tests the marker primitives directly without importlib.reload, which
        re-imports the client/cache chain and leaks state into later tests in
        the same process.
        """
        with tempfile.TemporaryDirectory() as marker_dir:
            marker = os.path.join(marker_dir, "shim.disabled")
            with patch.dict(os.environ, {"TOOLRECALL_SHIM_MARKER": marker}):
                # disable → marker present, predicate flips
                self.assertTrue(shim_mod.disable())
                self.assertTrue(os.path.isfile(shim_mod._marker_path()))
                self.assertTrue(shim_mod._marker_disabled())
                # enable → marker gone, predicate flips back
                self.assertTrue(shim_mod.enable())
                self.assertFalse(os.path.exists(shim_mod._marker_path()))
                self.assertFalse(shim_mod._marker_disabled())

    # ─── Bug 4 fix: pytest detection uses basename, not substring ───

    def test_apply_skips_when_pytest_in_argv(self):
        """apply() skips patching when pytest is detectable in-process.

        The reliable signal is ``"pytest" in sys.modules`` (python -m pytest
        sets argv[0] to pytest/__main__.py, which basename checks miss);
        the argv-based -m fallback stays as belt-and-braces. force=True is
        the documented escape hatch and must NOT bypass the argv check's
        purpose here, so this test exercises the default force=False path
        with PYTEST_CURRENT_TEST unset (sys.modules already has pytest).
        """
        orig_pytest = os.environ.pop("PYTEST_CURRENT_TEST", None)
        try:
            sys.argv = ["/usr/local/bin/pytest", "tests/"]
            shim_mod.remove()
            shim_mod.apply()  # force=False: pytest-in-sys.modules guard applies
            # Should NOT have patched — pytest detected
            self.assertIsNot(builtins.open, shim_mod._shim_open)
        finally:
            if orig_pytest is not None:
                os.environ["PYTEST_CURRENT_TEST"] = orig_pytest
            shim_mod.remove()

    def test_apply_patches_when_script_has_test_in_path(self):
        """apply() patches when script path contains 'test' but isn't pytest."""
        orig_argv = sys.argv[:]
        orig_pytest = os.environ.pop("PYTEST_CURRENT_TEST", None)
        orig_enabled = shim_mod._ENABLED
        shim_mod._ENABLED = True
        try:
            sys.argv = ["/home/user/my_test_script.py"]
            shim_mod.remove()
            shim_mod.apply(force=True)
            # Should have patched — 'test' in path but not pytest basename
            self.assertIs(builtins.open, shim_mod._shim_open)
        finally:
            sys.argv = orig_argv
            if orig_pytest is not None:
                os.environ["PYTEST_CURRENT_TEST"] = orig_pytest
            shim_mod._ENABLED = orig_enabled
            shim_mod.remove()


class TestShimLocalHitFastPath(unittest.TestCase):
    """W2 T2 (RED gate): a warm in-process entry must serve without IPC.

    The shim currently pays a daemon round trip on EVERY cache hit (~2.46 ms,
    bench/tr_shim_latency.py). W2 adds an in-process LRU + stat gate in
    ``_shim_open`` and targets p50 <= 0.2 ms. The only way that is sound is if
    a warm hit never talks to the client — so this test breaks the client on
    purpose and demands the read still succeed.

    The disk body DIFFERS from the cached body deliberately: a shim that
    silently falls through to the real ``open()`` would return the disk body
    and fail loudly here, instead of passing because both copies happen to
    match. (A false green here would ship a "fast path" that is just the
    slow path with the cache disabled.)
    """

    CACHED = "CACHED = 'served from the in-process entry'\n" * 40
    ON_DISK = "DISK = 'the real file on disk'\n" * 40
    ON_DISK_AFTER = "DISK = 'rewritten after the entry was warmed'\n" * 60

    def setUp(self):
        self._orig_tr = shim_mod._TR
        self._orig_original_open = shim_mod._original_open
        self._orig_skip_prefixes = shim_mod._SKIP_PREFIXES
        self._orig_enabled = shim_mod._ENABLED
        self._orig_agent = shim_mod._agent_process_cache
        self._orig_tag_mode = shim_mod._tag_source_mode_cache
        self._orig_stub = shim_mod._stub_maybe

        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "fixture.py")
        with open(self.path, "w", encoding="utf-8") as f:
            f.write(self.ON_DISK)

        shim_mod._SKIP_PREFIXES = []  # no config load inside the timed path
        shim_mod._ENABLED = True
        shim_mod._agent_process_cache = False  # no stub: content must be served
        shim_mod._tag_source_mode_cache = "never"
        shim_mod._stub_maybe = None
        shim_mod._thread_local.active = False
        self._reset_local_entry()

    def tearDown(self):
        shim_mod._TR = self._orig_tr
        shim_mod._original_open = self._orig_original_open
        shim_mod._SKIP_PREFIXES = self._orig_skip_prefixes
        shim_mod._ENABLED = self._orig_enabled
        shim_mod._agent_process_cache = self._orig_agent
        shim_mod._tag_source_mode_cache = self._orig_tag_mode
        shim_mod._stub_maybe = self._orig_stub
        shim_mod._local_supported = True
        shim_mod._thread_local.active = False
        try:
            os.unlink(self.path)
            os.rmdir(self.tmp)
        except OSError:
            pass

    def _reset_local_entry(self):
        """Drop any in-process entry if the fast path exists (T3+)."""
        reset = getattr(shim_mod, "local_cache_reset", None)
        if reset is not None:
            reset()

    def _serve_cached(self):
        def read(p, **kwargs):
            return {
                "cached": True,
                "content": self.CACHED,
                "lossless": True,
                "source": "shim",
            }

        return read

    def _explode(self, p, **kwargs):
        raise RuntimeError("IPC attempted on a warm local hit")

    def _read(self, mode="rb"):
        f = shim_mod._shim_open(self.path, mode)
        try:
            return f.read()
        finally:
            f.close()

    def test_warm_local_hit_serves_without_ipc(self):
        """Second read of an unchanged file must not reach the client at all."""
        shim_mod._TR = {"read": self._serve_cached()}
        self.assertEqual(self._read(), self.CACHED.encode(), "warm-up read failed")

        shim_mod._TR = {"read": self._explode}
        body = self._read()
        self.assertEqual(
            body,
            self.CACHED.encode(),
            "warm local hit did not serve from the in-process entry — it "
            "either called the client (IPC) or fell through to disk",
        )

    def test_changed_stat_never_serves_the_stale_entry(self):
        """mtime/size change must invalidate the local entry (correctness)."""
        shim_mod._TR = {"read": self._serve_cached()}
        self.assertEqual(self._read(), self.CACHED.encode(), "warm-up read failed")

        with open(self.path, "w", encoding="utf-8") as f:
            f.write(self.ON_DISK_AFTER)  # mtime + size both change

        # Cache miss from the daemon's point of view: the only correct answer
        # is the NEW bytes read from disk, never the warm entry.
        shim_mod._TR = {"read": lambda p, **kwargs: {"error": "miss"}}
        self.assertEqual(self._read(), self.ON_DISK_AFTER.encode())


class TestShimLocalHitAccounting(unittest.TestCase):
    """W2 T4: local hits must not make the counters smaller.

    The fast path answers warm reads in-process, so the daemon never sees
    them. Each served hit is queued and flushed in batches; the batch must
    carry every hit (32-hit threshold, timer, and atexit fallback), and the
    daemon-side recorder must move the counters by exactly the batch size.
    """

    BODY = "PAYLOAD = 'x'\n" * 200

    def setUp(self):
        self._orig_tr = shim_mod._TR
        self._orig_skip = shim_mod._SKIP_PREFIXES
        self._orig_enabled = shim_mod._ENABLED
        self._orig_agent = shim_mod._agent_process_cache
        self._orig_tag_mode = shim_mod._tag_source_mode_cache
        self._orig_stub = shim_mod._stub_maybe
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "fixture.py")
        with open(self.path, "w", encoding="utf-8") as f:
            f.write(self.BODY)
        shim_mod._SKIP_PREFIXES = []
        shim_mod._ENABLED = True
        shim_mod._agent_process_cache = False
        shim_mod._tag_source_mode_cache = "never"
        shim_mod._stub_maybe = None
        shim_mod._thread_local.active = False
        shim_mod.local_cache_reset()
        # This file points TOOLRECALL_CACHE_DB at a bare temp file; create the
        # schema so the counter assertions below measure real writes.
        from toolrecall import cache as _cache_mod

        _cache_mod._init()

    def tearDown(self):
        shim_mod.local_cache_reset()
        shim_mod._TR = self._orig_tr
        shim_mod._SKIP_PREFIXES = self._orig_skip
        shim_mod._ENABLED = self._orig_enabled
        shim_mod._agent_process_cache = self._orig_agent
        shim_mod._tag_source_mode_cache = self._orig_tag_mode
        shim_mod._stub_maybe = self._orig_stub
        shim_mod._local_supported = True
        shim_mod._thread_local.active = False
        try:
            os.unlink(self.path)
            os.rmdir(self.tmp)
        except OSError:
            pass

    def _read(self):
        f = shim_mod._shim_open(self.path, "rb")
        try:
            return f.read()
        finally:
            f.close()

    def test_threshold_flush_carries_every_hit(self):
        sent = []

        def read(p, **kwargs):
            return {"cached": True, "content": self.BODY, "lossless": True, "source": "shim"}

        shim_mod._TR = {"read": read, "send": sent.append}
        self.assertEqual(self._read(), self.BODY.encode())  # warms: 1 queued
        for _ in range(shim_mod._LOCAL_FLUSH_AT - 1):
            self.assertEqual(self._read(), self.BODY.encode())
        self.assertEqual(len(sent), 1, "batch must flush at the 32-hit threshold")
        self.assertEqual(sent[0]["cmd"], "local_hits")
        entries = sent[0]["entries"]
        self.assertEqual(len(entries), shim_mod._LOCAL_FLUSH_AT)
        self.assertTrue(all(e["tokens"] > 0 for e in entries))
        self.assertTrue(all(e["path"] == self.path for e in entries))

    def test_timer_and_pending_flush_lose_nothing(self):
        sent = []

        def read(p, **kwargs):
            return {"cached": True, "content": self.BODY, "lossless": True, "source": "shim"}

        shim_mod._TR = {"read": read, "send": sent.append}
        self._read()  # 1 queued, timer armed
        self._read()  # 2 queued
        shim_mod._local_flush_pending()  # flush-on-exit path
        self.assertEqual(len(sent), 1)
        self.assertEqual(len(sent[0]["entries"]), 2)

    def test_daemon_recorder_moves_counters_by_batch_size(self):
        from toolrecall import cache

        base = cache.get_stats().get("file_cache", {})
        h0 = base.get("hits", 0)
        io0 = base.get("io_tokens_saved", 0)
        ctx0 = base.get("tokens_saved_to_llm", 0)

        n = 5
        entries = [
            {"path": f"/tmp/localhit-{i}.py", "tokens": 120, "source": "agent_tool"}
            for i in range(n)
        ]
        self.assertEqual(cache.record_local_hits(entries), n)
        fc = cache.get_stats()["file_cache"]
        self.assertEqual(fc.get("hits", 0) - h0, n)
        self.assertEqual(fc.get("io_tokens_saved", 0) - io0, n * 120)
        self.assertEqual(fc.get("tokens_saved_to_llm", 0) - ctx0, n * 120)

    def test_bad_entry_is_skipped_not_fatal(self):
        from toolrecall import cache

        base = cache.get_stats().get("file_cache", {})
        h0 = base.get("hits", 0)
        n = cache.record_local_hits(
            [
                {"path": "", "tokens": 10},
                "not-a-dict",
                {"tokens": 5},
                {"path": "/tmp/ok.py", "tokens": 7},
            ]
        )
        self.assertEqual(n, 1)
        fc = cache.get_stats()["file_cache"]
        self.assertEqual(fc.get("hits", 0) - h0, 1)


class TestShimLocalHitSafety(unittest.TestCase):
    """W2 T5: the fast path must keep every existing safety property."""

    def setUp(self):
        self._orig_tr = shim_mod._TR
        self._orig_skip = shim_mod._SKIP_PREFIXES
        self._orig_enabled = shim_mod._ENABLED
        self._orig_agent = shim_mod._agent_process_cache
        self._orig_tag_mode = shim_mod._tag_source_mode_cache
        self._orig_stub = shim_mod._stub_maybe
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "fixture.py")
        self.body = "A = 1\n" * 100
        with open(self.path, "w", encoding="utf-8") as f:
            f.write(self.body)
        shim_mod._SKIP_PREFIXES = []
        shim_mod._ENABLED = True
        shim_mod._agent_process_cache = False
        shim_mod._tag_source_mode_cache = "never"
        shim_mod._stub_maybe = None
        shim_mod._thread_local.active = False
        shim_mod.local_cache_reset()

    def tearDown(self):
        shim_mod.local_cache_reset()
        shim_mod._TR = self._orig_tr
        shim_mod._SKIP_PREFIXES = self._orig_skip
        shim_mod._ENABLED = self._orig_enabled
        shim_mod._agent_process_cache = self._orig_agent
        shim_mod._tag_source_mode_cache = self._orig_tag_mode
        shim_mod._stub_maybe = self._orig_stub
        shim_mod._local_supported = True
        shim_mod._thread_local.active = False
        try:
            os.unlink(self.path)
            os.rmdir(self.tmp)
        except OSError:
            pass

    def _read(self):
        f = shim_mod._shim_open(self.path, "rb")
        try:
            return f.read()
        finally:
            f.close()

    def _serve(self):
        return lambda p, **kw: {
            "cached": True,
            "content": self.body,
            "lossless": True,
            "source": "agent_tool",
        }

    def test_stub_still_fires_on_a_local_hit(self):
        """agent_tool + warm entry -> stub note, still without IPC."""
        calls = {"n": 0}

        def fake_stub(path, content, is_agent=False, lossless=False):
            calls["n"] += 1
            return None if calls["n"] == 1 else "STUB: see earlier read\n"

        shim_mod._stub_maybe = fake_stub
        shim_mod._agent_process_cache = True
        shim_mod._tag_source_mode_cache = "always"
        shim_mod._TR = {"read": self._serve()}
        self.assertEqual(self._read(), self.body.encode())  # full first send
        shim_mod._TR = {"read": lambda p, **kw: (_ for _ in ()).throw(RuntimeError("IPC"))}
        self.assertEqual(self._read(), b"STUB: see earlier read\n")

    def test_changed_file_with_dead_daemon_never_serves_stale(self):
        """IPC gone AND file changed: the entry is invalid, so read from disk."""
        shim_mod._TR = {"read": self._serve()}
        self.assertEqual(self._read(), self.body.encode())

        new_body = "B = 2  # rewritten\n" * 120
        with open(self.path, "w", encoding="utf-8") as f:
            f.write(new_body)

        shim_mod._TR = {"read": lambda p, **kw: (_ for _ in ()).throw(RuntimeError("dead"))}
        self.assertEqual(self._read(), new_body.encode())

    def test_local_disabled_env_still_serves_via_daemon(self):
        os.environ["TOOLRECALL_SHIM_LOCAL"] = "0"
        try:
            reads = []

            def read(p, **kwargs):
                reads.append(p)
                return {"cached": True, "content": self.body, "lossless": True, "source": "shim"}

            shim_mod._TR = {"read": read}
            self.assertEqual(self._read(), self.body.encode())
            self.assertEqual(self._read(), self.body.encode())
            self.assertEqual(len(reads), 2, "kill switch must keep the old IPC path")
        finally:
            os.environ.pop("TOOLRECALL_SHIM_LOCAL", None)

    def test_unknown_command_reverts_to_ipc(self):
        """An old daemon must downgrade the process, not blind the counters."""
        reads = []

        def read(p, **kwargs):
            reads.append(p)
            return {"cached": True, "content": self.body, "lossless": True, "source": "shim"}

        shim_mod._TR = {
            "read": read,
            "send": lambda payload: {"error": "Unknown command: local_hits"},
        }
        for _ in range(shim_mod._LOCAL_FLUSH_AT + 1):
            self._read()
        self.assertFalse(
            shim_mod._local_enabled(),
            "must downgrade to the IPC path after an unknown-command reply",
        )
        before = len(reads)
        self._read()
        self.assertEqual(
            len(reads), before + 1, "reads must go back through the daemon once downgraded"
        )

    def test_lru_evicts_and_unlinks(self):
        orig_cap = shim_mod._LOCAL_MAX_ENTRIES
        shim_mod._LOCAL_MAX_ENTRIES = 2

        def localhit_files():
            return {
                n
                for n in os.listdir(tempfile.gettempdir())
                if n.startswith(shim_mod._LOCAL_TMP_PREFIX)
            }

        before = localhit_files()
        try:
            for i in range(3):
                p = os.path.join(self.tmp, f"evict{i}.py")
                with open(p, "w", encoding="utf-8") as f:
                    f.write(f"X = {i}\n")
                shim_mod._TR = {
                    "read": lambda q, **kw: {
                        "cached": True,
                        "content": "X = 1\n",
                        "lossless": True,
                        "source": "shim",
                    }
                }
                fh = shim_mod._shim_open(p, "rb")
                fh.close()
            self.assertLessEqual(len(shim_mod._local_entries), 2)
            # Every surviving entry points at a real temp file; the evicted one
            # was unlinked, so the third file must not still be on disk.
            for entry in shim_mod._local_entries.values():
                self.assertTrue(os.path.exists(entry["tmp"]))
            live = {os.path.basename(e["tmp"]) for e in shim_mod._local_entries.values()}
            leftovers = (localhit_files() - before) - live
            self.assertEqual(leftovers, set(), f"evicted temp files were not unlinked: {leftovers}")
        finally:
            shim_mod._LOCAL_MAX_ENTRIES = orig_cap


if __name__ == "__main__":
    unittest.main()
