"""api_cache retention: expired rows must actually be reclaimed, and oversize
bodies must never be stored.

Background (2026-09-26): ``garbage_collect()`` pruned ``terminal_cache``,
``mcp_cache`` and ``recall_cache`` but **not** ``api_cache``, whose rows expire
after ``API_CACHE_TTL`` (300 s). Measured on the live DB: 12,254 of 12,359 rows
were expired and held ~1.09 GB of a 1.2 GB file — the DB was ~91 % dead weight,
and writes into it (commit + auto-checkpoint) were the remaining stall class on
the cache hot path. Oversize bodies are a separate lever: the proxy tees *billed*
LLM responses into api_cache, so one enormous row must not be able to bloat the
file. Bodies are skipped whole, never truncated — a truncated body would be
replayed as a hit and would be a corrupt response to the caller.
"""

import time

import pytest

from toolrecall import cache


@pytest.fixture(autouse=True)
def _schema():
    """The hermetic cache DB starts empty — create the schema first."""
    cache._init()


def _row(h: str, expires_in: float):
    """(sql, params) inserting an api_cache row that expires in ``expires_in`` s."""
    now = time.time()
    return (
        "INSERT OR REPLACE INTO api_cache (request_hash, method, host, path,"
        " request_body_hash, request_body_preview, response_status,"
        " response_headers, response_body, cached_at, expires_at, hit_streak)"
        " VALUES (?, 'POST', 'api.example.com', '/v1/x', 'bodyhash', 'preview',"
        " 200, '{}', 'body', ?, ?, 0)",
        (h, now, now + expires_in),
    )


def _hashes():
    with cache._db() as conn:
        return sorted(r[0] for r in conn.execute("SELECT request_hash FROM api_cache"))


class _Cfg:
    """Minimal config stub exposing only the key under test."""

    def __init__(self, cap_mb):
        self.cap_mb = cap_mb

    def get(self, section, key, default=None, **kw):
        if (section, key) == ("cache", "api_cache_max_body_mb"):
            return self.cap_mb
        return default


def test_gc_reclaims_expired_api_rows_and_keeps_live_ones():
    with cache._db() as conn:
        for h, exp in (("dead1", -300), ("dead2", -1), ("live1", +300)):
            conn.execute(*_row(h, exp))
    assert len(_hashes()) == 3

    cache.garbage_collect()

    assert _hashes() == ["live1"], "expired api rows purged, live rows kept"


def test_gc_return_counts_reclaimed_api_rows():
    with cache._db() as conn:
        for h in ("dead3", "dead4"):
            conn.execute(*_row(h, -60))
    n = cache.garbage_collect()
    assert n >= 2, f"GC should report reclaimed rows, got {n}"


def test_gc_survives_when_only_live_rows_are_present():
    with cache._db() as conn:
        conn.execute(*_row("live2", +300))
    assert cache.garbage_collect() >= 0
    assert _hashes() == ["live2"]


def test_oversize_body_is_not_stored(monkeypatch):
    monkeypatch.setattr(cache, "config", _Cfg(cap_mb=0.001))  # 1 KB cap
    body = "x" * 2048
    out = cache.cached_api_store("key-big", "POST", "api.example.com", "/v1/x", "bh", 200, {}, body)
    assert out["stored"] is False
    assert out["reason"] == "body_too_large"
    assert out["bytes"] == len(body)
    with cache._db() as conn:
        n = conn.execute(
            "SELECT COUNT(*) FROM api_cache WHERE request_hash = 'key-big'"
        ).fetchone()[0]
    assert n == 0, "an oversize body must not reach the DB at all"


def test_body_within_the_cap_is_stored(monkeypatch):
    monkeypatch.setattr(cache, "config", _Cfg(cap_mb=0.001))  # 1 KB cap
    out = cache.cached_api_store(
        "key-ok", "POST", "api.example.com", "/v1/x", "bh", 200, {}, "x" * 1024
    )
    assert out["stored"] is True
    assert _hashes() == ["key-ok"]


def test_sqlite_backend_takes_checkpoints_off_the_commit_path(tmp_path):
    """A1: no auto-checkpoint on commit — the daemon's 60 s thread bounds the WAL."""
    from toolrecall import config as config_mod
    from toolrecall.storage import sqlite as backend

    conn = backend.connect(config_mod.load_config(), str(tmp_path / "pragmas.db"))
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert conn.execute("PRAGMA wal_autocheckpoint").fetchone()[0] == 0
    finally:
        conn.close()
