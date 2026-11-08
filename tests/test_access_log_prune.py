"""Access-log prune: correctness, gating, and the perf regression guard.

Background (2026-09-25): `_record()` pruned the access log on EVERY read with a
path, as

    DELETE FROM access_log WHERE id NOT IN (
        SELECT id FROM access_log ORDER BY cached_at DESC LIMIT 50000)

`access_log` sits at its 50k-row cap, so that subquery sorted 50,000 rows per
read: 45.3 ms for the subquery alone, 86.4 ms per read on a 1.1 GB live DB. It
made a cache HIT cost the same as a cold read (~43 ms vs ~44 ms measured). The
prune is now interval-gated and prunes by primary key.

These tests pin the three properties that matter: the gate, the key-based form,
and the resulting latency (a full-table sort would blow the perf assertion).
"""

import time

import pytest

from toolrecall import cache


class _Cfg:
    """Minimal config stub: isolates these tests from env/import-time config."""

    def __init__(self, keep=10, backend="sqlite"):
        self.keep = keep
        self.backend = backend

    def get(self, section, key, default=None, **kw):
        if (section, key) == ("cache", "access_log_keep"):
            return self.keep
        if (section, key) == ("storage", "backend"):
            return self.backend
        return default

    def storage_backend(self):
        return self.backend


@pytest.fixture
def hermetic(monkeypatch, tmp_path):
    """Point cache at a stub config and a fresh schema; keep the gates open."""
    cfg = _Cfg()
    monkeypatch.setattr(cache, "config", cfg)
    monkeypatch.setattr(cache, "_LAST_INTEGRITY_CHECK", time.time())  # no thread
    cache._init()
    return cfg


def _fill(n, start=0):
    with cache._db() as conn:
        conn.executemany(
            "INSERT INTO access_log (category, path, hit, tokens, cached_at, client) "
            "VALUES ('file_cache', ?, 1, 1, ?, 'test')",
            [(f"/tmp/f{i}", time.time()) for i in range(start, start + n)],
        )


def _count():
    with cache._db() as conn:
        return conn.execute("SELECT count(*) FROM access_log").fetchone()[0]


def _ids():
    with cache._db() as conn:
        return [r[0] for r in conn.execute("SELECT id FROM access_log ORDER BY id")]


def _record(path="/tmp/probe"):
    cache._record(
        "file_cache", hit=True, path=path, tokens_saved=1, context_tokens=1, client="test"
    )


def test_prune_is_gated_and_keeps_newest_rows(hermetic):
    """Within the interval: no prune. Once due: keep the newest N, by id."""
    _fill(50)
    assert _count() == 50

    cache._LAST_ACCESS_PRUNE = time.time()  # just pruned
    _record()
    assert _count() == 51, "prune must not run inside the interval"

    cache._LAST_ACCESS_PRUNE = 0.0  # due
    _record()  # inserts a row, then prunes
    kept = _ids()
    assert len(kept) == hermetic.keep, f"expected {hermetic.keep} rows, got {len(kept)}"
    newest = max(kept)
    # the survivors must be the newest N ids, i.e. a contiguous suffix
    assert kept == list(range(newest - hermetic.keep + 1, newest + 1)), kept


def test_prune_statement_uses_primary_key_not_a_sort(hermetic):
    """The prune must not sort the table — that was the 45-86 ms cost."""
    seen = []
    with cache._db() as conn:
        conn.set_trace_callback(seen.append)
    try:
        cache._LAST_ACCESS_PRUNE = 0.0
        _record()
    finally:
        with cache._db() as conn:
            conn.set_trace_callback(None)

    deletes = [s for s in seen if s.strip().upper().startswith("DELETE") and "access_log" in s]
    assert deletes, "expected the prune DELETE to be executed"
    stmt = deletes[0]
    assert "max(id)" in stmt, f"prune should bound by primary key: {stmt!r}"
    assert "ORDER BY" not in stmt.upper(), f"prune must not sort: {stmt!r}"
    assert "NOT IN" not in stmt.upper(), f"prune must not use NOT IN: {stmt!r}"


def test_record_latency_with_a_full_access_log(hermetic, monkeypatch):
    """Regression guard: a full 50k-row log must not make _record slow.

    Old behaviour: ~45-86 ms per record (full sort). New: sub-millisecond when
    gated. The bound is deliberately loose (shared CI boxes) but far below the
    old cost, so a reintroduced sort fails here.
    """
    monkeypatch.setattr(cache, "_ACCESS_LOG_KEEP", 50000)
    hermetic.keep = 50000
    _fill(50000)

    cache._LAST_ACCESS_PRUNE = time.time()  # gated: no prune
    t0 = time.perf_counter()
    for _ in range(5):
        _record()
    gated_ms = (time.perf_counter() - t0) / 5 * 1000

    cache._LAST_ACCESS_PRUNE = 0.0  # prune actually runs
    t0 = time.perf_counter()
    _record()
    prune_ms = (time.perf_counter() - t0) * 1000

    assert gated_ms < 25, f"gated _record too slow: {gated_ms:.1f} ms"
    assert prune_ms < 25, f"prune itself too slow: {prune_ms:.1f} ms"
    assert _count() == 50000
