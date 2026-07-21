"""Tests for ContextTracker.get_stale() — read-then-overwritten detection.

The distinction under test:
  dirty  = written since checkpoint (may never have been read)
  clean  = read, never written        (content still correct)
  stale  = read, THEN written         (content in context is wrong)
"""

import os
import tempfile

import pytest

from toolrecall.context_tracker import ContextTracker


@pytest.fixture
def tmpfile():
    fd, path = tempfile.mkstemp(suffix=".py")
    with os.fdopen(fd, "w") as f:
        f.write("x" * 4000)
    yield path
    try:
        os.unlink(path)
    except OSError:
        pass


def test_written_but_never_read_is_not_stale(tmpfile):
    """No stale copy exists in context if the agent never read it."""
    t = ContextTracker()
    t.mark_dirty(tmpfile)
    result = t.get_stale()
    assert result["total_stale"] == 0
    assert result["paths"] == []
    # ...but it IS dirty, per the existing model.
    assert tmpfile in t.get_dirty()["dirty"]


def test_read_never_written_is_not_stale(tmpfile):
    """Clean content is droppable but not wrong."""
    t = ContextTracker()
    t.mark_read(tmpfile)
    assert t.get_stale()["total_stale"] == 0
    assert tmpfile in t.get_dirty()["clean"]


def test_read_then_written_is_stale(tmpfile):
    """The core case: agent saw version A, disk now holds version B."""
    t = ContextTracker()
    t.mark_read(tmpfile)
    t.mark_dirty(tmpfile)

    result = t.get_stale()
    assert result["total_stale"] == 1
    assert result["paths"] == [tmpfile]

    entry = result["stale"][0]
    assert entry["read_seq"] < entry["write_seq"]
    assert entry["est_tokens"] == 1000  # 4000 bytes // 4


def test_reread_after_write_clears_staleness(tmpfile):
    """Re-reading means the agent has seen current bytes."""
    t = ContextTracker()
    t.mark_read(tmpfile)
    t.mark_dirty(tmpfile)
    assert t.get_stale()["total_stale"] == 1

    t.mark_read(tmpfile)
    assert t.get_stale()["total_stale"] == 0


def test_write_after_reread_is_stale_again(tmpfile):
    """Staleness is a live property, not a one-shot latch."""
    t = ContextTracker()
    t.mark_read(tmpfile)
    t.mark_dirty(tmpfile)
    t.mark_read(tmpfile)
    t.mark_dirty(tmpfile)
    assert t.get_stale()["total_stale"] == 1


def test_deleted_file_still_reported_stale(tmpfile):
    """A file deleted after reading is maximally stale."""
    t = ContextTracker()
    t.mark_read(tmpfile)
    t.mark_dirty(tmpfile)
    os.unlink(tmpfile)

    result = t.get_stale()
    assert result["total_stale"] == 1
    assert result["stale"][0]["size"] == 0
    assert result["stale"][0]["mtime"] is None


def test_reclaimable_tokens_sums_across_files():
    paths = []
    t = ContextTracker()
    try:
        for _ in range(3):
            fd, p = tempfile.mkstemp()
            with os.fdopen(fd, "w") as f:
                f.write("y" * 400)
            paths.append(p)
            t.mark_read(p)
            t.mark_dirty(p)

        result = t.get_stale()
        assert result["total_stale"] == 3
        assert result["est_reclaimable_tokens"] == 300  # 3 * (400 // 4)
    finally:
        for p in paths:
            try:
                os.unlink(p)
            except OSError:
                pass


def test_reset_clears_stale_state(tmpfile):
    t = ContextTracker()
    t.mark_read(tmpfile)
    t.mark_dirty(tmpfile)
    t.reset()
    result = t.get_stale()
    assert result["total_stale"] == 0
    assert result["seq"] == 0


def test_stale_is_independent_of_checkpoints(tmpfile):
    """Staleness is about read/write order, not checkpoint boundaries.

    This is the behaviour the checkpoint model could not express:
    a checkpoint taken between the read and the write does not make
    the context copy correct again.
    """
    t = ContextTracker()
    t.mark_read(tmpfile)
    t.set_checkpoint("mid")
    t.mark_dirty(tmpfile)
    assert t.get_stale()["total_stale"] == 1
