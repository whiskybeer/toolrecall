"""
Tests for the Context Tracker — in-memory dirty-file tracking.

Tests cover:
  - Basic operations (set_checkpoint, mark_dirty, get_dirty)
  - Dirty/clean distinction
  - Read tracking
  - Multiple checkpoints
  - Reset
  - Edge cases (empty paths, repeated operations)
  - Thread safety
  - Integration with daemon IPC
"""

import os
import sys
import time
import tempfile
import threading

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from toolrecall.context_tracker import ContextTracker


# ─── Fixtures ─────────────────────────────────────────────


def make_temp_file(content: str = "test") -> str:
    """Create a temporary file and return its path."""
    f = tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".txt")
    f.write(content)
    f.close()
    return f.name


# ─── Tests ────────────────────────────────────────────────


class TestContextTrackerBasics:
    """Core operations — set_checkpoint, mark_dirty, get_dirty."""

    def test_empty_tracker(self):
        """Fresh tracker: no dirty, no clean, checkpoint = 0."""
        ct = ContextTracker()
        assert ct.checkpoint == 0

        stats = ct.get_stats()
        assert stats["total_dirty"] == 0
        assert stats["total_clean"] == 0
        assert stats["total_read"] == 0
        assert stats["checkpoint"] == 0

    def test_checkpoint_creates_new_id(self):
        """Each set_checkpoint increments the checkpoint ID."""
        ct = ContextTracker()
        r1 = ct.set_checkpoint("first")
        assert r1["checkpoint"] == 1
        assert r1["name"] == "first"

        r2 = ct.set_checkpoint("second")
        assert r2["checkpoint"] == 2
        assert r2["name"] == "second"

    def test_dirty_file_appears_in_dirty_list(self):
        """After mark_dirty, file appears in dirty list."""
        ct = ContextTracker()
        ct.set_checkpoint("start")

        path = make_temp_file()
        try:
            ct.mark_dirty(path)
            result = ct.get_dirty()
            assert path in result["dirty"]
            assert result["total_dirty"] == 1
            assert result["total_clean"] == 0
        finally:
            os.unlink(path)

    def test_dirty_and_clean_distinction(self):
        """Read files are clean, written files are dirty."""
        ct = ContextTracker()
        ct.set_checkpoint("start")

        read_path = make_temp_file("read")
        write_path = make_temp_file("write")
        try:
            ct.mark_read(read_path)
            ct.mark_dirty(write_path)

            result = ct.get_dirty()
            assert read_path in result["clean"]
            assert write_path in result["dirty"]
            assert result["total_dirty"] == 1
            assert result["total_clean"] == 1
        finally:
            os.unlink(read_path)
            os.unlink(write_path)

    def test_read_only_is_clean(self):
        """Files that are only read (never written) are clean."""
        ct = ContextTracker()
        ct.set_checkpoint("start")

        path = make_temp_file()
        try:
            ct.mark_read(path)
            ct.mark_read(path)  # Multiple reads — still clean

            result = ct.get_dirty()
            assert path in result["clean"]
            assert path not in result["dirty"]
            assert result["total_dirty"] == 0
            assert result["total_clean"] == 1
        finally:
            os.unlink(path)

    def test_rewritten_file_is_dirty(self):
        """File that was read then written becomes dirty, not clean."""
        ct = ContextTracker()
        ct.set_checkpoint("start")

        path = make_temp_file()
        try:
            ct.mark_read(path)
            ct.mark_dirty(path)  # Written after read

            result = ct.get_dirty()
            assert path in result["dirty"]
            assert path not in result["clean"]
            assert result["total_dirty"] == 1
            assert result["total_clean"] == 0
        finally:
            os.unlink(path)

    def test_multiple_dirty_files(self):
        """Multiple writes → multiple dirty files."""
        ct = ContextTracker()
        ct.set_checkpoint("start")

        paths = [make_temp_file() for _ in range(5)]
        try:
            for p in paths:
                ct.mark_dirty(p)

            result = ct.get_dirty()
            assert result["total_dirty"] == 5
            for p in paths:
                assert p in result["dirty"]
        finally:
            for p in paths:
                os.unlink(p)

    def test_checkpoint_does_not_clear(self):
        """set_checkpoint does not auto-clear dirty/read state.
        The dirty list persists until the agent processes it or calls reset.
        """
        ct = ContextTracker()
        ct.set_checkpoint("start")

        path = make_temp_file()
        try:
            ct.mark_dirty(path)
            ct.set_checkpoint("still_dirty")  # Checkpoint just marks time

            result = ct.get_dirty()
            assert result["total_dirty"] == 1
            assert path in result["dirty"]
        finally:
            os.unlink(path)


class TestContextTrackerReset:
    """Reset and edge cases."""

    def test_reset_clears_all(self):
        """Reset clears dirty, clean, and resets checkpoint to 0."""
        ct = ContextTracker()
        ct.set_checkpoint("start")
        path = make_temp_file()
        try:
            ct.mark_dirty(path)
            ct.mark_read(make_temp_file())

            result = ct.reset()
            assert result["reset"] is True
            assert result["checkpoint"] == 0

            stats = ct.get_stats()
            assert stats["total_dirty"] == 0
            assert stats["total_clean"] == 0
            assert stats["total_read"] == 0
        finally:
            os.unlink(path)

    def test_reset_then_reuse(self):
        """After reset, tracker works fresh."""
        ct = ContextTracker()
        ct.set_checkpoint("first")
        ct.mark_dirty(make_temp_file())
        ct.reset()

        result = ct.get_dirty()
        assert result["total_dirty"] == 0
        assert result["total_clean"] == 0

    def test_empty_path_mark_dirty(self):
        """Marking empty path is a no-op."""
        ct = ContextTracker()
        ct.set_checkpoint("start")
        ct.mark_dirty("")
        ct.mark_dirty(None)  # type: ignore

        result = ct.get_dirty()
        assert result["total_dirty"] == 0

    def test_empty_path_mark_read(self):
        """Marking empty path as read is a no-op."""
        ct = ContextTracker()
        ct.set_checkpoint("start")
        ct.mark_read("")
        ct.mark_read(None)  # type: ignore

        result = ct.get_dirty()
        assert result["total_clean"] == 0

    def test_nonexistent_file_dirty(self):
        """Dirty file that doesn't exist still appears (mtime fallback)."""
        ct = ContextTracker()
        ct.set_checkpoint("start")
        ct.mark_dirty("/nonexistent/path/file.txt")

        result = ct.get_dirty()
        assert "/nonexistent/path/file.txt" in result["dirty"]
        # The mark_dirty catches OSError and falls back to time.time()

    def test_get_dirty_with_specific_checkpoint(self):
        """get_dirty(checkpoint=N) returns state relative to that checkpoint."""
        ct = ContextTracker()
        cp1 = ct.set_checkpoint("before_read")["checkpoint"]

        path = make_temp_file()
        try:
            ct.mark_read(path)
            cp2 = ct.set_checkpoint("after_read")["checkpoint"]

            ct.mark_dirty(path)

            # Check against checkpoint 1 (before_read): should include
            # both the read and the write
            result_cp1 = ct.get_dirty(checkpoint=cp1)
            assert result_cp1["total_dirty"] == 1

            # Check against checkpoint 2 (after_read): should still
            # see the dirty (it happened after cp2)
            result_cp2 = ct.get_dirty(checkpoint=cp2)
            assert result_cp2["total_dirty"] == 1
        finally:
            os.unlink(path)

    def test_no_checkpoint_set(self):
        """Without any checkpoint, get_dirty still works."""
        ct = ContextTracker()
        path = make_temp_file()
        try:
            ct.mark_dirty(path)
            result = ct.get_dirty()
            assert path in result["dirty"]
        finally:
            os.unlink(path)


class TestContextTrackerThreadSafety:
    """Multi-threaded access should not corrupt state."""

    def test_concurrent_mark_dirty(self):
        """Multiple threads can mark_dirty simultaneously."""
        ct = ContextTracker()
        ct.set_checkpoint("start")

        paths = [make_temp_file() for _ in range(20)]

        def _mark(p):
            for _ in range(100):
                ct.mark_dirty(p)
                ct.mark_read(p)

        threads = [threading.Thread(target=_mark, args=(p,)) for p in paths]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        result = ct.get_dirty()
        assert result["total_dirty"] == 20

        for p in paths:
            os.unlink(p)

    def test_concurrent_read_and_reset(self):
        """Concurrent reads and resets should not deadlock."""
        ct = ContextTracker()
        ct.set_checkpoint("start")

        path = make_temp_file()
        events = []

        def _read_write():
            for _ in range(50):
                ct.mark_read(path)
                ct.mark_dirty(path)
                events.append(1)

        def _reset():
            for _ in range(10):
                ct.reset()
                ct.set_checkpoint("after_reset")
                events.append(2)

        t1 = threading.Thread(target=_read_write)
        t2 = threading.Thread(target=_reset)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        # Should not crash or deadlock
        result = ct.get_stats()
        assert "dirty" in result
        os.unlink(path)


class TestContextTrackerViaDaemonIPC:
    """Integration-style tests — verify IPC works end-to-end.
    
    These require a running daemon. If daemon is not available,
    they test the client functions' fallback behavior.
    """

    def test_client_functions_exist(self):
        """Context tracker client functions are importable."""
        from toolrecall.client import (
            context_set_checkpoint,
            context_get_dirty,
            context_get_stats,
            context_reset,
        )
        assert callable(context_set_checkpoint)
        assert callable(context_get_dirty)
        assert callable(context_get_stats)
        assert callable(context_reset)

    def test_client_without_daemon(self):
        """Client functions handle daemon-not-running gracefully."""
        # Stop the daemon if it's running (ignore if not installed / no daemon)
        import subprocess
        try:
            subprocess.run(["toolrecall", "daemon", "--stop"], capture_output=True)
        except FileNotFoundError:
            pass
        time.sleep(0.5)

        from toolrecall.client import (
            context_set_checkpoint,
            context_get_dirty,
            context_reset,
        )

        result = context_set_checkpoint("no_daemon_test")
        assert isinstance(result, dict)
        # Should return error about daemon, not crash

        result = context_get_dirty()
        assert isinstance(result, dict)

        result = context_reset()
        assert isinstance(result, dict)