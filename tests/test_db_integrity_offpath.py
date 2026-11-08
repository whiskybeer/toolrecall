"""The periodic DB integrity check must never block the request path.

Background (2026-09-25): `_ensure_db_integrity()` ran `PRAGMA integrity_check`
inline — on the caller's thread and on the shared `_db()` connection. On a
1.1 GB DB that measured 15.6-20.0 s, so the unlucky read that triggered it froze
the whole daemon request loop for that long (live log: `cached_read` peaks at
30.5 s). It now runs in a daemon thread on its own read-only connection.

These tests pin: the caller is not blocked, the shared DB lock is not held, at
most one check runs at a time, remote backends are skipped, and the check uses
its own read-only connection.
"""

import sqlite3
import threading
import time

import pytest

from toolrecall import cache


class _Cfg:
    """Minimal config stub (see test_access_log_prune for rationale)."""

    def __init__(self, backend="sqlite", db_path=""):
        self.backend = backend
        self.db_path = db_path

    def get(self, section, key, default=None, **kw):
        if (section, key) == ("storage", "db_path"):
            return self.db_path
        if (section, key) == ("storage", "backend"):
            return self.backend
        return default

    def storage_backend(self):
        return self.backend


def _wait_for(pred, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.01)
    return False


@pytest.fixture
def gate(monkeypatch, tmp_path):
    """Config stub pointing at a temp DB, with the check gate forced due."""
    cfg = _Cfg(db_path=str(tmp_path / "hermetic.db"))
    monkeypatch.setattr(cache, "config", cfg)
    monkeypatch.setattr(cache, "_LAST_INTEGRITY_CHECK", 0.0)
    monkeypatch.setattr(cache, "_INTEGRITY_CHECK_IN_PROGRESS", False)
    return cfg


def test_schedules_on_a_background_thread_and_returns_immediately(gate, monkeypatch):
    started = threading.Event()
    runs = []

    def fake_worker(db_path):
        started.set()
        time.sleep(0.2)
        runs.append(db_path)
        cache._INTEGRITY_CHECK_IN_PROGRESS = False

    monkeypatch.setattr(cache, "_run_integrity_check", fake_worker)

    t0 = time.perf_counter()
    cache._ensure_db_integrity()
    elapsed = time.perf_counter() - t0

    assert elapsed < 0.1, f"caller was blocked for {elapsed * 1000:.1f} ms"
    assert started.wait(2.0), "the check never started"
    assert _wait_for(lambda: runs == [gate.db_path]), f"worker did not run: {runs}"


def test_holds_no_db_lock_while_checking(gate, monkeypatch):
    """A long check must not stall ordinary DB calls (shared singleton lock)."""
    release = threading.Event()

    def fake_worker(db_path):
        release.wait(3.0)
        cache._INTEGRITY_CHECK_IN_PROGRESS = False

    monkeypatch.setattr(cache, "_run_integrity_check", fake_worker)

    # Warm the path first: this test measures "not blocked by the check", not the
    # one-time schema init / first-open cost (which can exceed the budget when a
    # previous test file in the same session has just churned the DB).
    with cache._db() as conn:
        conn.execute("SELECT 1").fetchone()

    cache._ensure_db_integrity()
    time.sleep(0.05)  # let the worker start

    t0 = time.perf_counter()
    with cache._db() as conn:
        conn.execute("SELECT 1").fetchone()
    elapsed = time.perf_counter() - t0
    release.set()

    assert elapsed < 0.2, f"_db() was blocked for {elapsed * 1000:.1f} ms"


def test_only_one_check_runs_at_a_time(gate, monkeypatch):
    release = threading.Event()
    started = threading.Event()
    runs = []

    def fake_worker(db_path):
        runs.append(db_path)
        started.set()
        release.wait(3.0)
        cache._INTEGRITY_CHECK_IN_PROGRESS = False

    monkeypatch.setattr(cache, "_run_integrity_check", fake_worker)
    cache._ensure_db_integrity()
    assert started.wait(2.0), "the first check never started"

    cache._LAST_INTEGRITY_CHECK = 0.0  # due again while a check is in flight
    cache._ensure_db_integrity()
    time.sleep(0.1)
    release.set()

    assert len(runs) == 1, f"checks stacked instead of being skipped: {runs}"


def test_non_sqlite_backend_is_skipped(monkeypatch, tmp_path):
    cfg = _Cfg(backend="libsql", db_path=str(tmp_path / "remote.db"))
    monkeypatch.setattr(cache, "config", cfg)
    monkeypatch.setattr(cache, "_LAST_INTEGRITY_CHECK", 0.0)
    monkeypatch.setattr(cache, "_INTEGRITY_CHECK_IN_PROGRESS", False)
    runs = []
    monkeypatch.setattr(cache, "_run_integrity_check", lambda p: runs.append(p))

    cache._ensure_db_integrity()
    time.sleep(0.1)

    assert runs == [], "a remote backend must not be checked by file path"
    assert cache._INTEGRITY_CHECK_IN_PROGRESS is False


def test_check_opens_its_own_read_only_connection(gate, monkeypatch):
    events = []

    class _FakeCursor:
        def fetchone(self):
            return ("ok",)

    class _FakeConn:
        def execute(self, sql):
            events.append(("exec", sql))
            return _FakeCursor()

        def close(self):
            events.append(("close", None))

    def fake_connect(*args, **kw):
        events.append(("connect", args[0] if args else kw.get("database"), kw))
        return _FakeConn()

    monkeypatch.setattr(cache.sqlite3, "connect", fake_connect)
    cache._run_integrity_check(gate.db_path)

    connects = [e for e in events if e[0] == "connect"]
    assert connects, "the check must open its own connection"
    _, uri, kw = connects[0]
    assert uri.startswith("file:") and "mode=ro" in uri, uri
    assert kw.get("uri") is True
    assert any(e[0] == "close" for e in events), "the connection must be closed"
    assert cache._INTEGRITY_CHECK_IN_PROGRESS is False


def test_malformed_database_triggers_recovery(gate, monkeypatch):
    recovered = []
    monkeypatch.setattr(cache, "_recover_database", lambda p: recovered.append(p))

    def boom(uri, **kw):
        raise sqlite3.OperationalError("database disk image is malformed")

    monkeypatch.setattr(cache.sqlite3, "connect", boom)

    with pytest.warns(UserWarning, match="corruption"):
        cache._run_integrity_check(gate.db_path)

    assert recovered == [gate.db_path]
    assert cache._INTEGRITY_CHECK_IN_PROGRESS is False
