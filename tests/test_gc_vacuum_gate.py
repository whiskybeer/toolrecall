"""The 4-hourly GC must not pay a multi-second VACUUM for nothing — and must not
fail silently.

``VACUUM`` rewrites the whole file while holding the write lock, and its cost
scales with FILE size rather than with what it reclaims (measured on the live DB:
2,668 ms for a 98 MB file, ≈27 ms/MB). Run unconditionally every 4 h it is a
multi-second freeze waiting to coincide with real traffic, so it is now gated on
the reclaimable share (``_VACUUM_MIN_FREE_MB`` / ``_VACUUM_MIN_FREE_RATIO``, with
``[cache] vacuum_min_free_mb`` overriding the floor).
"""

import contextlib
import sqlite3
import time

import pytest

from toolrecall import cache, daemon


@pytest.fixture(autouse=True)
def _schema():
    """The hermetic cache DB starts empty — create the schema first."""
    cache._init()


# Captured before any patching: the spy must wrap the real context manager, not
# itself (patching cache._db and then calling cache._db() recursed to death).
_REAL_DB = cache._db


class _RecConn:
    """Records every SQL string executed; delegates everything else."""

    def __init__(self, conn):
        self._c = conn
        self.sql = []

    def execute(self, sql, *args):
        self.sql.append(sql if isinstance(sql, str) else str(sql))
        return self._c.execute(sql, *args)

    def __getattr__(self, name):
        return getattr(self._c, name)


@contextlib.contextmanager
def _spy_db(state):
    """Same contract as cache._db(), but hands out a recording wrapper."""
    with _REAL_DB() as conn:
        if state.get("conn") is None:
            state["conn"] = _RecConn(conn)
        yield state["conn"]


class _Cfg:
    """Minimal config stub: overrides only the VACUUM floor."""

    def __init__(self, floor_mb):
        self.floor_mb = floor_mb

    def get(self, section, key, default=None, **kw):
        if (section, key) == ("cache", "vacuum_min_free_mb"):
            return self.floor_mb
        return default


def _churn_free_space(blob_bytes=1048576):
    """Leave the file mostly free pages: write a blob, then delete it."""
    with _REAL_DB() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS _gc_probe (b BLOB)")
        conn.execute("INSERT INTO _gc_probe (b) VALUES (?)", ("x" * blob_bytes,))
        conn.commit()
        conn.execute("DELETE FROM _gc_probe")
        conn.execute("DROP TABLE _gc_probe")
        conn.commit()


def _free_and_size():
    with _REAL_DB() as conn:
        ps = conn.execute("PRAGMA page_size").fetchone()[0]
        free = conn.execute("PRAGMA freelist_count").fetchone()[0] * ps
        size = conn.execute("PRAGMA page_count").fetchone()[0] * ps
    return free, size


def _vacuum_calls(conn):
    return [s for s in conn.sql if s.strip().upper().startswith("VACUUM")]


def test_gc_skips_vacuum_when_little_is_reclaimable(monkeypatch):
    """The absolute floor means a small DB never pays the rewrite."""
    _churn_free_space()
    free, size = _free_and_size()
    assert free > 0, "test setup: expected free pages after the delete"

    state = {}
    monkeypatch.setattr(cache, "_db", lambda: _spy_db(state))
    # An expired api_cache row must still be reclaimed even when VACUUM is skipped.
    with _REAL_DB() as conn:
        now = time.time()
        conn.execute(
            "INSERT INTO api_cache (request_hash, method, host, path, request_body_hash,"
            " request_body_preview, response_status, response_headers, response_body,"
            " cached_at, expires_at, hit_streak) VALUES ('gate-probe','POST','h','/p','b',"
            " 'p',200,'{}','body',?,?,0)",
            (now, now - 60),
        )
        conn.commit()

    result = cache.garbage_collect()

    assert _vacuum_calls(state["conn"]) == [], (
        f"{free} free of {size} bytes is below the default floor — must not VACUUM"
    )
    assert result >= 1, f"purges must still run when VACUUM is skipped, got {result}"
    with _REAL_DB() as conn:
        left = conn.execute(
            "SELECT COUNT(*) FROM api_cache WHERE request_hash='gate-probe'"
        ).fetchone()[0]
    assert left == 0, "the expired row must be reclaimed regardless of VACUUM"


def test_gc_vacuums_when_reclaimable_share_is_worth_it(monkeypatch):
    """With the floor removed, a mostly-free file does get compacted."""
    _churn_free_space()
    free, size = _free_and_size()
    monkeypatch.setattr(cache, "config", _Cfg(floor_mb=0.0))

    state = {}
    monkeypatch.setattr(cache, "_db", lambda: _spy_db(state))
    cache.garbage_collect()

    assert _vacuum_calls(state["conn"]), (
        f"free {free} of {size} bytes exceeds the {cache._VACUUM_MIN_FREE_RATIO:.0%} "
        "share threshold — should have VACUUMed"
    )


class _Log:
    def __init__(self):
        self.warnings = []

    def warning(self, msg, *args):
        self.warnings.append(msg % args if args else msg)

    def info(self, *args, **kwargs):
        pass


def test_gc_loop_failure_is_visible(monkeypatch):
    """A failing GC must be logged, not swallowed.

    Silence is how the api_cache backlog accumulated for weeks: 12,281 expired
    rows holding 1,103 MB of a 1,190 MB file, with nothing in the log.
    """
    d = daemon.DaemonServer.__new__(daemon.DaemonServer)
    d._running = True
    d.logger = _Log()

    ticks = []

    def fake_sleep(_seconds):
        ticks.append(1)
        if len(ticks) >= 14400:  # the 4-hour sleep loop, collapsed
            d._running = False

    def boom():
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(daemon.time, "sleep", fake_sleep)
    monkeypatch.setattr("toolrecall.cache.garbage_collect", boom)

    d._run_periodic_gc()  # must not raise

    assert any("GC failed" in w for w in d.logger.warnings), (
        f"a failing GC must be logged, got {d.logger.warnings}"
    )
