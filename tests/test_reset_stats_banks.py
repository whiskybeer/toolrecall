"""reset_stats() must MOVE statistics, never destroy them (2026-10-22).

The function used to end in

    DELETE FROM cache_stats
    DELETE FROM access_log

so `toolrecall reset-stats` (and every benchmark that calls it) silently threw
away lifetime counters *and* the whole trend window. The trend is computed from
access_log at query time and has no other source, so a reset blanked
`toolrecall trend` — the same silent-loss class as the GC's unbanked reset
found on 2026-10-03.

These tests pin the move: counters land in cache_stats_banked (live window
zeroed, lifetime intact, including the measured bridge-stub axis the GC
deliberately leaves alone), and the access_log window is compacted into
access_log_banked so the daily trend still reaches back past the reset.
"""

import os
import sqlite3
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DAY = 86400
SEED = {
    "hits": 400,
    "misses": 25,
    "tokens_read_from_disk": 9000,
    "io_tokens_saved": 555555,
    "tokens_saved_to_llm": 44444,
    "tokens_read_uncacheable": 777,
    "tokens_not_sent_measured": 123456,
}


@pytest.fixture(autouse=True)
def _fresh_db(_isolated_toolrecall_env):
    """Per-test cache DB + fresh _db() singleton (see conftest)."""
    import toolrecall._db as dbmod

    dbmod._cached_config = None
    if getattr(dbmod, "_db_real", None) is not None:
        try:
            dbmod._db_real.close()
        except Exception:
            pass
        dbmod._db_real = None
    from toolrecall.cache import SCHEMA

    dbmod._init(SCHEMA)
    yield
    if getattr(dbmod, "_db_real", None) is not None:
        try:
            dbmod._db_real.close()
        except Exception:
            pass
        dbmod._db_real = None
        dbmod._cached_config = None


def _seed_counters(category: str = "file_cache") -> None:
    from toolrecall import cache

    with cache._db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO cache_stats (category, hits, misses,"
            " tokens_read_from_disk, io_tokens_saved, tokens_saved_to_llm,"
            " tokens_read_uncacheable, tokens_not_sent_measured, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (category, *SEED.values(), time.time()),
        )


def _seed_reads(
    path: str, hits: int, misses: int, days_ago: int, category: str = "file_cache"
) -> None:
    """Two access_log rows (one hit row per hit, one miss row per miss)."""
    from toolrecall import cache

    ts = time.time() - days_ago * DAY
    with cache._db() as conn:
        for _ in range(hits):
            conn.execute(
                "INSERT INTO access_log (category, path, hit, tokens, cached_at, client)"
                " VALUES (?, ?, 1, 100, ?, '')",
                (category, path, ts),
            )
        for _ in range(misses):
            conn.execute(
                "INSERT INTO access_log (category, path, hit, tokens, cached_at, client)"
                " VALUES (?, ?, 0, 100, ?, '')",
                (category, path, ts),
            )


def _live(category: str = "file_cache") -> dict:
    from toolrecall import cache

    with cache._db() as conn:
        row = conn.execute(
            "SELECT hits, misses, io_tokens_saved, tokens_saved_to_llm,"
            " tokens_not_sent_measured, tokens_read_uncacheable"
            " FROM cache_stats WHERE category = ?",
            (category,),
        ).fetchone()
    keys = (
        "hits",
        "misses",
        "io_tokens_saved",
        "tokens_saved_to_llm",
        "tokens_not_sent_measured",
        "tokens_read_uncacheable",
    )
    return dict(zip(keys, row)) if row else {}


def _banked(category: str = "file_cache") -> dict:
    from toolrecall import cache

    with cache._db() as conn:
        row = conn.execute(
            "SELECT hits, misses, io_tokens_saved, tokens_saved_to_llm,"
            " tokens_not_sent_measured, tokens_read_uncacheable, bank_count"
            " FROM cache_stats_banked WHERE category = ?",
            (category,),
        ).fetchone()
    keys = (
        "hits",
        "misses",
        "io_tokens_saved",
        "tokens_saved_to_llm",
        "tokens_not_sent_measured",
        "tokens_read_uncacheable",
        "bank_count",
    )
    return dict(zip(keys, row)) if row else {}


def test_reset_moves_counters_and_zeroes_the_window():
    from toolrecall import cache

    _seed_counters()
    cache.reset_stats()

    live, bank = _live(), _banked()
    assert live["hits"] == 0 and live["io_tokens_saved"] == 0, "window resets"
    assert bank["hits"] == SEED["hits"], "the total must be moved, not dropped"
    assert bank["misses"] == SEED["misses"]
    assert bank["io_tokens_saved"] == SEED["io_tokens_saved"]
    assert bank["tokens_saved_to_llm"] == SEED["tokens_saved_to_llm"]
    assert bank["bank_count"] == 1


def test_measured_and_uncacheable_axes_survive_a_reset():
    """The two axes the GC deliberately leaves alone must still be banked here.

    reset_stats() zeroes the whole row, so leaving them out would erase the
    only wire-level verified savings figure (bridge stubs) from every report.
    """
    from toolrecall import cache

    _seed_counters()
    cache.reset_stats()

    bank = _banked()
    assert bank["tokens_not_sent_measured"] == SEED["tokens_not_sent_measured"]
    assert bank["tokens_read_uncacheable"] == SEED["tokens_read_uncacheable"]

    fc = cache.get_stats()["file_cache"]
    assert fc["lifetime_tokens_not_sent_measured"] == SEED["tokens_not_sent_measured"]
    assert fc["lifetime_tokens_read_uncacheable"] == SEED["tokens_read_uncacheable"]
    assert fc["tokens_not_sent_measured"] == 0, "live window is the reset one"


def test_reset_keeps_the_lifetime_totals_readable():
    from toolrecall import cache

    _seed_counters()
    cache.reset_stats()

    fc = cache.get_stats()["file_cache"]
    assert fc["lifetime_hits"] == SEED["hits"]
    assert fc["lifetime_io_tokens_saved"] == SEED["io_tokens_saved"]
    assert fc["lifetime_tokens_saved_to_llm"] == SEED["tokens_saved_to_llm"]


def test_cache_entries_are_untouched():
    """The documented contract of reset-stats still holds."""
    from toolrecall import cache

    with cache._db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO file_cache"
            " (path_hash, path, content, mtime, size, cached_at)"
            " VALUES ('h', '/tmp/kept.txt', 'body', 1.0, 4, ?)",
            (time.time(),),
        )
    cache.reset_stats()
    with cache._db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM file_cache").fetchone()[0] == 1


def test_trend_window_survives_a_reset():
    """The whole point: `toolrecall trend` must not go blank."""
    from toolrecall import cache, trend

    _seed_reads("/home/u/project/a.py", hits=10, misses=2, days_ago=3)
    before = trend.daily_trend(days=None)
    assert [d["reads"] for d in before] == [12]

    cache.reset_stats()

    after = trend.daily_trend(days=None)
    assert after == before, "a reset must not change what the trend reports"
    cov = trend.coverage()
    assert cov["banked_days"] == 1
    assert cov["banked_reads"] == 12
    assert cov["banked_hits"] == 10


def test_live_reads_add_on_top_of_banked_days():
    """Banked and live rows are disjoint: same day, both counted once."""
    from toolrecall import cache, trend

    _seed_reads("/home/u/project/a.py", hits=4, misses=1, days_ago=2)
    cache.reset_stats()
    _seed_reads("/home/u/project/a.py", hits=6, misses=3, days_ago=2)

    rows = trend.daily_trend(days=None)
    assert len(rows) == 1
    assert rows[0]["reads"] == 14, "4+1 banked plus 6+3 live"
    assert rows[0]["hits"] == 10


def test_banked_rows_obey_the_infra_filter():
    """The filter must apply to banked rows too, or a reset would leak
    agent-plumbing back into the user-facing trend (the 2026-09 plateau)."""
    from toolrecall import cache, trend

    _seed_reads("/home/u/.hermes/config.yaml", hits=1, misses=9, days_ago=1)
    _seed_reads("/home/u/project/real.py", hits=5, misses=0, days_ago=1)
    cache.reset_stats()

    rows = trend.daily_trend(days=None)
    assert len(rows) == 1
    assert rows[0]["reads"] == 5, "the 10 infra reads stay out"
    assert rows[0]["hits"] == 5


def test_miss_classes_see_banked_paths():
    from toolrecall import cache, trend

    _seed_reads("/home/u/project/misses.py", hits=1, misses=200, days_ago=4)
    cache.reset_stats()

    classes = trend.miss_classes(min_reads=100)
    assert [c["reads"] for c in classes] == [201]
    assert classes[0]["hit_pct"] < 1.0


def test_two_resets_accumulate_instead_of_overwriting():
    from toolrecall import cache

    _seed_counters()
    cache.reset_stats()
    _seed_counters()
    cache.reset_stats()

    bank = _banked()
    assert bank["hits"] == SEED["hits"] * 2
    assert bank["bank_count"] == 2


def test_access_log_is_compacted_not_left_behind():
    from toolrecall import cache

    _seed_reads("/home/u/project/a.py", hits=3, misses=1, days_ago=1)
    cache.reset_stats()

    with sqlite3.connect(f"file:{os.environ['TOOLRECALL_CACHE_DB']}?mode=ro", uri=True) as conn:
        live = conn.execute("SELECT COUNT(*) FROM access_log").fetchone()[0]
        banked = conn.execute("SELECT COUNT(*) FROM access_log_banked").fetchone()[0]
    assert live == 0, "the raw window is emptied (that is the reset)"
    assert banked == 1, "but the counts moved into the compacted table"
