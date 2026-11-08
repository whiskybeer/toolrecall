"""Per-file hit counter + honest hit-rate reporting (2026-10-27).

Two defects, one theme — numbers that could not be trusted:

1. ``file_cache.hits`` was stored at 1 and NEVER incremented, while
   ``bench/cache_honest.py`` builds its whole report on it: ``hits > 1``
   meant "re-served from SQLite" and ``(hits) × size/4`` the re-read savings.
   With the counter frozen, every re-read was invisible — the "honest cache
   report" reported none.
2. ``get_stats()`` reported ``hit_rate = "0%"`` for an empty window. An empty
   window observed nothing; "0%" claims a measured all-miss rate. Right after
   a ``reset_stats()`` that reads "0%" next to a ``lifetime_*`` value of 96% —
   same number, opposite meanings, no way to tell which one you got.
"""

import os
import sys
import tempfile
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


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


def _write_file(content: str = "hello cache\n") -> str:
    fd, path = tempfile.mkstemp(suffix=".txt", text=True)
    with os.fdopen(fd, "w") as fh:
        fh.write(content)
    return path


def _row_hits(path: str) -> int:
    from toolrecall import cache

    with cache._db() as conn:
        row = conn.execute("SELECT hits FROM file_cache WHERE path = ?", (path,)).fetchone()
    return row[0] if row else -1


def test_repeated_reads_increment_the_persisted_counter():
    """1 = stored from disk, each serve adds one — the column finally moves."""
    from toolrecall import cache

    path = _write_file()
    cache.cached_read(path)
    assert _row_hits(path) == 1, "the initial disk read is recorded at store time"

    cache.cached_read(path)  # served from the memory LRU
    assert _row_hits(path) == 2, "a re-read must bump the persisted counter"

    cache.cached_read(path)  # served again
    assert _row_hits(path) == 3


def test_cache_honest_classification_works_again():
    """The report's once-only / re-read split needs a moving counter."""
    from toolrecall import cache

    p1 = _write_file("a\n")
    p2 = _write_file("b\n")
    cache.cached_read(p1)
    cache.cached_read(p2)
    cache.cached_read(p2)  # re-read

    with cache._db() as conn:
        rows = conn.execute("SELECT path, hits FROM file_cache").fetchall()
    once_only = sum(1 for _p, h in rows if h == 1)
    re_read = sum(1 for _p, h in rows if h > 1)
    assert (once_only, re_read) == (1, 1), "one disk-only file, one re-served file"

    # cache_honest's savings estimate: (hits - 1) × size // 4 per file
    saved = sum(max(h - 1, 0) * (len(open(p).read()) // 4) for p, h in rows)
    assert saved == (len("b\n") // 4), "only the re-served file contributes"


def test_empty_window_reads_dash_not_zero():
    """hit_rate == 0% claimed a measurement; an empty window observed none."""
    from toolrecall import cache

    with cache._db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO cache_stats (category, hits, misses, updated_at)"
            " VALUES ('file_cache', 0, 0, ?)",
            (time.time(),),
        )

    fc = cache.get_stats()["file_cache"]
    assert fc["hits"] == 0 and fc["misses"] == 0
    assert fc["hit_rate"] == "-", "an empty window must not claim 0% hit rate"
    assert fc["lifetime_hit_rate"] == "-", "no banked history either"


def test_lifetime_hit_rate_survives_a_reset():
    """The pair is the point: window '-', lifetime still 96% — readable, not
    two meanings of the same number."""
    from toolrecall import cache

    with cache._db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO cache_stats (category, hits, misses, updated_at)"
            " VALUES ('file_cache', 96, 4, ?)",
            (time.time(),),
        )

    cache.reset_stats()

    fc = cache.get_stats()["file_cache"]
    assert fc["hits"] == 0 and fc["misses"] == 0, "window was reset"
    assert fc["hit_rate"] == "-"
    assert fc["lifetime_hit_rate"] == "96%", "the total must stay readable"


def test_hit_rate_still_reports_a_populated_window():
    """A window with traffic keeps a real percentage (no over-correction)."""
    from toolrecall import cache

    with cache._db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO cache_stats (category, hits, misses, updated_at)"
            " VALUES ('file_cache', 80, 20, ?)",
            (time.time(),),
        )

    fc = cache.get_stats()["file_cache"]
    assert fc["hit_rate"] == "80%"
    assert fc["lifetime_hit_rate"] == "80%"
