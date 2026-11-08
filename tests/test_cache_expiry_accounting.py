# Copyright (c) 2026 Robin Schultka
# SPDX-License-Identifier: MIT
# Source: https://github.com/whiskybeer/toolrecall
"""Browser-cache expiry and IO-offload accounting (fixed 2026-09-28).

Three defects, one class each side of the same mistake — a guard that was
computed and then dropped:

  A — ``browser_cache`` rows never expired. ``BROWSER_CACHE_TTL`` existed but
  nothing read it, so a cached page was served for the lifetime of the DB:
  a silent stale delivery of point-in-time content.

  B — nothing purged ``browser_cache``. With no expiry and no reclaimer the
  table only ever grew (the same shape the api_cache purge fixed for that
  table).

  C — ``browser_cache`` and ``terminal_cache`` hits recorded
  ``io_tokens_saved=0``. Both computed the token estimate and dropped it, so
  the TUI IO-offload column read 0 despite real replays — the same bug class
  as the api_cache (2026-09-21) and mcp_cache (2026-09-24) fixes.
"""

from __future__ import annotations

import time
import unittest

from toolrecall import cache as cache_mod


class TestBrowserExpiry(unittest.TestCase):
    def setUp(self) -> None:
        cache_mod._init()
        with cache_mod._db() as conn:
            conn.execute("DELETE FROM cache_stats")
            conn.execute("DELETE FROM browser_cache")

    def _store_at(self, key: str, body: str, age_s: float) -> None:
        cache_mod.cached_browser_store(key, body, url="https://example.com")
        with cache_mod._db() as conn:
            conn.execute(
                "UPDATE browser_cache SET cached_at = ? WHERE cache_key = ?",
                (time.time() - age_s, key),
            )

    def test_fresh_row_is_served(self) -> None:
        self._store_at("fresh", "hello", age_s=10)
        hit = cache_mod.cached_browser_check("fresh")
        assert hit.get("cached") is True, hit
        assert hit.get("content") == "hello"

    def test_expired_row_is_a_miss(self) -> None:
        """Bug A: an expired page must not be served."""
        self._store_at("old", "hello", age_s=cache_mod.BROWSER_CACHE_TTL + 60)
        hit = cache_mod.cached_browser_check("old")
        assert hit.get("cached") is False, f"expired page served: {hit}"

    def test_browser_hit_records_tokens(self) -> None:
        """Bug C: the estimate must ride the hit record."""
        self._store_at("tok", "x" * 3000, age_s=1)
        hit = cache_mod.cached_browser_check("tok")
        assert hit.get("cached") is True, hit
        with cache_mod._db() as conn:
            row = conn.execute(
                "SELECT io_tokens_saved FROM cache_stats WHERE category='browser_cache'"
            ).fetchone()
        assert row is not None, "browser_cache stats row missing"
        assert row[0] > 0, f"browser hit recorded io_tokens_saved={row[0]}"


class TestGarbageCollectReclaimsBrowser(unittest.TestCase):
    """Bug B: the GC must reclaim expired browser rows and keep fresh ones."""

    def setUp(self) -> None:
        cache_mod._init()
        with cache_mod._db() as conn:
            conn.execute("DELETE FROM browser_cache")

    def test_purges_expired_and_keeps_fresh(self) -> None:
        now = time.time()
        with cache_mod._db() as conn:
            for key, age in (("dead", cache_mod.BROWSER_CACHE_TTL + 120), ("live", 30)):
                conn.execute(
                    "INSERT OR REPLACE INTO browser_cache "
                    "(cache_key, url, content_type, content, title, content_hash, cached_at) "
                    "VALUES (?, '', 'text', 'x', '', '', ?)",
                    (key, now - age),
                )
        cache_mod.garbage_collect()
        with cache_mod._db() as conn:
            left = {r[0] for r in conn.execute("SELECT cache_key FROM browser_cache")}
        assert "live" in left, "GC removed a fresh browser row"
        assert "dead" not in left, "GC kept an expired browser row"

    def test_stats_reset_does_not_touch_browser_rows(self) -> None:
        """The stats reset is a separate concern from the row purge."""
        now = time.time()
        with cache_mod._db() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO browser_cache "
                "(cache_key, url, content_type, content, title, content_hash, cached_at) "
                "VALUES ('fresh', '', 'text', 'x', '', '', ?)",
                (now - 5,),
            )
        cache_mod.garbage_collect()
        with cache_mod._db() as conn:
            (n,) = conn.execute("SELECT COUNT(*) FROM browser_cache").fetchone()
        assert n == 1, f"fresh row lost in GC: {n} rows"


class TestTerminalHitAccounting(unittest.TestCase):
    """Bug C, terminal side."""

    def setUp(self) -> None:
        cache_mod._init()
        with cache_mod._db() as conn:
            conn.execute("DELETE FROM cache_stats")
            conn.execute("DELETE FROM terminal_cache")

    def test_terminal_hit_records_tokens(self) -> None:
        # "whoami" is in DEFAULT_CACHEABLE (read-only, static) — a genuinely
        # cacheable command, so this exercises a real hit rather than skipping.
        first = cache_mod.cached_terminal("whoami", ttl=300)
        assert not first.get("error"), first
        hit = cache_mod.cached_terminal("whoami", ttl=300)
        assert hit.get("cached") is True, f"expected a cache hit: {hit}"
        assert hit.get("output") == first.get("output")
        with cache_mod._db() as conn:
            row = conn.execute(
                "SELECT io_tokens_saved FROM cache_stats WHERE category='terminal_cache'"
            ).fetchone()
        assert row is not None, "terminal_cache stats row missing"
        assert row[0] > 0, f"terminal hit recorded io_tokens_saved={row[0]}"


if __name__ == "__main__":
    unittest.main()
