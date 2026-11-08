"""The GC silently destroyed lifetime counters (found 2026-10-03).

`garbage_collect()` ended its purge block with:

    UPDATE cache_stats SET hits = 0, misses = 0,
        tokens_read_from_disk = 0, io_tokens_saved = 0, updated_at = ?
    WHERE updated_at > 0 AND updated_at < ?          -- older than one day

so any category idle for 24 h lost its lifetime counters — and `updated_at = now`
was written in the same statement, which made the row look freshly active
afterwards. Nothing was logged.

Observed on the live DB: `file_cache` reported 551,803 hits / 2.02 B
`io_tokens_saved` before and 1,675 hits / 1.9 M after, while
`tokens_saved_to_llm` (14.5 M) and `tokens_not_sent_measured` (17.4 M) were
untouched and kept growing. That asymmetry is the fingerprint: the display then
claimed MORE context savings than IO offload, which is impossible by
construction (ctx ⊆ io). The historical totals are unrecoverable — the only
backup (cache.db.bak, 2026-07-26) predates the columns.

Origin: the statement dates from 36d27d0 (2026-07-04) — not from the GC/VACUUM
work (d57d45f) and not from the 2026-09-28 rename, which only renamed the column
inside it. No test covered it, which is why three tests below exist now: the GC
may stop counting, but it must bank what it resets and say so.
"""

import logging
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TWO_DAYS = 2 * 86400
SEED = {
    "hits": 1000,
    "misses": 50,
    "tokens_read_from_disk": 12345,
    "io_tokens_saved": 999999,
    "tokens_saved_to_llm": 4242,
}


@pytest.fixture(autouse=True)
def _reset_db_singleton(_isolated_toolrecall_env):
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


def _seed_idle_row(category: str = "file_cache", idle_s: float = TWO_DAYS) -> None:
    from toolrecall import cache

    with cache._db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO cache_stats (category, hits, misses,"
            " tokens_read_from_disk, io_tokens_saved, tokens_saved_to_llm,"
            " updated_at, tokens_read_uncacheable, tokens_not_sent_measured)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, 0, 0)",
            (
                category,
                SEED["hits"],
                SEED["misses"],
                SEED["tokens_read_from_disk"],
                SEED["io_tokens_saved"],
                SEED["tokens_saved_to_llm"],
                time.time() - idle_s,
            ),
        )


def _live(category: str = "file_cache") -> dict:
    from toolrecall import cache

    with cache._db() as conn:
        r = conn.execute(
            "SELECT hits, misses, tokens_read_from_disk, io_tokens_saved,"
            " tokens_saved_to_llm FROM cache_stats WHERE category = ?",
            (category,),
        ).fetchone()
    keys = ("hits", "misses", "tokens_read_from_disk", "io_tokens_saved", "tokens_saved_to_llm")
    return dict(zip(keys, r)) if r else {}


def _banked(category: str = "file_cache") -> dict:
    from toolrecall import cache

    with cache._db() as conn:
        r = conn.execute(
            "SELECT hits, misses, tokens_read_from_disk, io_tokens_saved,"
            " tokens_saved_to_llm, bank_count FROM cache_stats_banked WHERE category = ?",
            (category,),
        ).fetchone()
    keys = (
        "hits",
        "misses",
        "tokens_read_from_disk",
        "io_tokens_saved",
        "tokens_saved_to_llm",
        "bank_count",
    )
    return dict(zip(keys, r)) if r else {}


def test_gc_banks_the_totals_it_resets():
    """The reset is allowed — destroying the total is not."""
    from toolrecall import cache

    _seed_idle_row()
    cache.garbage_collect()
    live, bank = _live(), _banked()
    assert live["hits"] == 0, "live row is reset (intended)"
    assert bank["hits"] == SEED["hits"], "the total must have been moved, not dropped"
    assert bank["misses"] == SEED["misses"]
    assert bank["io_tokens_saved"] == SEED["io_tokens_saved"]
    assert bank["tokens_read_from_disk"] == SEED["tokens_read_from_disk"]
    # The ctx axis is reset by the same statement, so it must be banked too.
    # Leaving it out made the lifetime view report MORE context savings than IO
    # offload — impossible, since ctx savings are a subset of IO offload.
    assert live["tokens_saved_to_llm"] == 0, "live ctx row is reset too"
    assert bank["tokens_saved_to_llm"] == SEED["tokens_saved_to_llm"]
    assert bank["bank_count"] == 1


def test_lifetime_view_survives_the_reset():
    """A reader asking for the total still gets the total."""
    from toolrecall import cache

    _seed_idle_row()
    cache.garbage_collect()
    fc = cache.get_stats()["file_cache"]
    assert fc["lifetime_hits"] == SEED["hits"]
    assert fc["lifetime_io_tokens_saved"] == SEED["io_tokens_saved"]
    assert fc["banked_io_tokens_saved"] == SEED["io_tokens_saved"]
    assert fc["lifetime_tokens_saved_to_llm"] == SEED["tokens_saved_to_llm"]
    assert fc["banked_tokens_saved_to_llm"] == SEED["tokens_saved_to_llm"]
    assert fc["bank_count"] == 1


def test_banking_never_double_counts():
    """Two GC passes must not bank the same counts twice: the reset bumps
    updated_at, so the row is no longer idle."""
    from toolrecall import cache

    _seed_idle_row()
    cache.garbage_collect()
    cache.garbage_collect()
    assert _banked()["bank_count"] == 1
    assert cache.get_stats()["file_cache"]["lifetime_io_tokens_saved"] == SEED["io_tokens_saved"]


def test_gc_logs_the_reset():
    """Silence is what made this invisible for three months."""
    from toolrecall import cache

    _seed_idle_row()
    records = []
    handler = logging.Handler()
    handler.emit = records.append
    logger = logging.getLogger("toolrecall.cache")
    logger.addHandler(handler)
    try:
        cache.garbage_collect()
    finally:
        logger.removeHandler(handler)
    assert any("banked" in r.getMessage() for r in records), "a reset must be visible in the log"


def test_savings_axes_stay_consistent():
    """ctx ⊆ io must hold in the lifetime view too."""
    from toolrecall import cache

    _seed_idle_row()
    cache.garbage_collect()
    fc = cache.get_stats()["file_cache"]
    assert fc["tokens_saved_to_llm"] <= fc["lifetime_io_tokens_saved"]
    # The invariant has to survive the reset on BOTH axes: if the GC zeroes io
    # but not ctx, lifetime ctx outgrows lifetime io (the 2026-10-22 live bug:
    # ctx 74.2M vs io 65.0M).
    assert fc["lifetime_tokens_saved_to_llm"] <= fc["lifetime_io_tokens_saved"], (
        "lifetime ctx savings can never exceed lifetime IO offload"
    )


def test_bank_table_gains_ctx_column_on_legacy_db():
    """A bank table created before the ctx column existed must be upgraded.

    CREATE TABLE IF NOT EXISTS is a no-op on an existing table, so the column
    has to be added explicitly — otherwise banked ctx totals would silently
    vanish from the lifetime view on every upgraded database.
    """
    import toolrecall._db as dbmod
    from toolrecall.cache import SCHEMA

    # Rebuild the bank table in its pre-2026-10-22 shape (no ctx column).
    with dbmod._db() as conn:
        conn.execute("DROP TABLE IF EXISTS cache_stats_banked")
        conn.execute(
            "CREATE TABLE cache_stats_banked ("
            " category TEXT PRIMARY KEY,"
            " hits INTEGER NOT NULL DEFAULT 0,"
            " misses INTEGER NOT NULL DEFAULT 0,"
            " tokens_read_from_disk INTEGER NOT NULL DEFAULT 0,"
            " io_tokens_saved INTEGER NOT NULL DEFAULT 0,"
            " banked_at REAL,"
            " bank_count INTEGER NOT NULL DEFAULT 0)"
        )

    dbmod._init(SCHEMA)

    with dbmod._db() as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(cache_stats_banked)")}
    assert "tokens_saved_to_llm" in cols, "legacy bank table must gain the ctx column"

    # And the GC must be able to bank into it without raising.
    _seed_idle_row()
    from toolrecall import cache as cache_mod

    cache_mod.garbage_collect()
    assert _banked()["tokens_saved_to_llm"] == SEED["tokens_saved_to_llm"]


def test_active_categories_are_untouched():
    """The original intent — don't keep stale numbers warm — still holds for
    active categories, and is not implemented by deleting history."""
    from toolrecall import cache

    _seed_idle_row(idle_s=60)  # updated a minute ago
    cache.garbage_collect()
    live = _live()
    assert live["hits"] == SEED["hits"]
    assert live["io_tokens_saved"] == SEED["io_tokens_saved"]
    assert _banked() == {}
