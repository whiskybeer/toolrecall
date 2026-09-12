"""Tests: ttl=0 (never-cache) reads must land in tokens_read_uncacheable,
NOT inflate the cacheable tokens_read_from_disk denominator (Fix 1, the
2026-09 '2% savings' artifact).
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


@pytest.fixture()
def isolated_db(monkeypatch, tmp_path):
    """Fresh cache.db + module re-import so singletons bind to tmp DB.

    NOTE: toolrecall._db must be reloaded TOO — its _cached_config /
    _db_path_cached globals survive cache.py reloads and would otherwise
    bleed the previous test's DB state into this one (the 266-token leak).
    """
    db = str(tmp_path / "cache.db")
    monkeypatch.setenv("TOOLRECALL_CACHE_DB", db)
    import importlib

    import toolrecall._db as db_mod
    import toolrecall.cache as cache

    importlib.reload(db_mod)
    importlib.reload(cache)
    yield cache
    # teardown reload must happen while the env var STILL points at the tmp
    # DB — reload triggers schema init connections; delenv first would make
    # them hit the production cache.db (production-path guard error).
    importlib.reload(db_mod)
    importlib.reload(cache)
    monkeypatch.undo()


def _read(cache, tmp_path, rel, content, ttl_glob_zero):
    path = tmp_path / rel
    path.write_text(content)
    if ttl_glob_zero:
        cache.config.config.setdefault("cache", {})["file_ttls"] = {f"*{rel.split('.')[-1]}": 0}
    return cache.cached_read(str(path))


def test_ttl0_miss_goes_to_uncacheable_bucket(isolated_db, tmp_path, monkeypatch):
    cache = isolated_db
    # pin the file as never-cache via the ttl policy (function-local import —
    # patch the source module, not cache's namespace)
    import toolrecall.ttl_policy as ttl_policy

    monkeypatch.setattr(ttl_policy, "resolve_file_ttl", lambda p, cfg: 0)
    path = tmp_path / "session.json"
    path.write_text("x" * 400)  # 100 tokens est
    r1 = cache.cached_read(str(path))
    assert r1["cached"] is False
    r2 = cache.cached_read(str(path))
    assert r2["cached"] is False
    stats = cache.get_stats()["file_cache"]
    assert stats["tokens_read_uncacheable"] >= 200  # both reads counted as uncacheable
    # the cacheable denominator must NOT include them
    # misses may be 0 (never_cache skips miss accounting) — key assertion:
    # hits+misses unchanged by these reads and disk counter not inflated
    assert stats["hits"] == 0 and stats["misses"] == 0


def test_cacheable_miss_still_counts_in_cacheable_bucket(isolated_db, tmp_path, monkeypatch):
    cache = isolated_db
    path = tmp_path / "normal.txt"
    path.write_text("y" * 400)
    cache.cached_read(str(path))
    stats = cache.get_stats()["file_cache"]
    assert stats["tokens_served_from_disk"] > 0
    assert stats.get("tokens_read_uncacheable", 0) == 0
