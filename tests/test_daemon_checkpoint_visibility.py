"""A failing periodic WAL checkpoint must be visible, never silent.

Since the commit path stopped auto-checkpointing (``storage/sqlite.py`` sets
``wal_autocheckpoint=0``), the daemon's periodic thread is the only thing that
bounds the WAL. Before this test the loop swallowed failures with a bare
``except Exception: pass`` — an unbounded ``-wal`` with no signal at all.
"""

import sqlite3

from toolrecall import daemon


class _Ctx:
    def snapshot(self):
        pass


class _Log:
    def __init__(self):
        self.warnings = []
        self.debugs = []

    def warning(self, msg, *args):
        self.warnings.append(msg % args if args else msg)

    def debug(self, msg, *args, **kwargs):
        self.debugs.append(msg % args if args else msg)


def _bare_daemon():
    """A DaemonServer with no sockets/threads — just what the loop touches."""
    d = daemon.DaemonServer.__new__(daemon.DaemonServer)
    d._running = True
    d._context = _Ctx()
    d.logger = _Log()
    return d


def _stop_after_one_tick(monkeypatch, d):
    """Make the loop's 60 x sleep(1) a no-op that ends the run after one tick."""
    ticks = []

    def fake_sleep(_seconds):
        ticks.append(1)
        if len(ticks) >= 60:
            d._running = False

    monkeypatch.setattr(daemon.time, "sleep", fake_sleep)
    return ticks


def test_checkpoint_failure_is_logged_not_swallowed(monkeypatch):
    d = _bare_daemon()
    _stop_after_one_tick(monkeypatch, d)

    def boom(**_kwargs):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr("toolrecall._db.db_checkpoint", boom)

    d._run_periodic_checkpoint()  # must not raise

    assert d.logger.warnings, "a failed checkpoint must produce a warning"
    assert any("checkpoint failed" in w for w in d.logger.warnings)
    assert any("disk I/O error" in w for w in d.logger.warnings)


def test_successful_checkpoint_is_quiet(monkeypatch):
    d = _bare_daemon()
    _stop_after_one_tick(monkeypatch, d)
    seen = []

    monkeypatch.setattr("toolrecall._db.db_checkpoint", lambda **kw: seen.append(kw))

    d._run_periodic_checkpoint()

    assert seen and seen[0].get("truncate") is True, "checkpoint must still truncate"
    assert d.logger.warnings == [], "success must not warn"
