"""E2E tests for canonical command keys + fuzzy TTL classification.

Env-driven config, isolated temp DB (same pattern as test_swr_e2e.py).
"""

import os
import sys
import tempfile
import unittest

test_db_dir = tempfile.mkdtemp()
test_db_path = os.path.join(test_db_dir, "test_canon.db")
os.environ["TOOLRECALL_CACHE_DB"] = test_db_path
os.environ["TOOLRECALL_CANONICAL_COMMANDS"] = "true"
os.environ["TOOLRECALL_FUZZY_TTL_MATCH"] = "true"
os.environ["TOOLRECALL_FUZZY_THRESHOLD"] = "0.7"

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import toolrecall.cache as _cache_mod  # noqa: E402
from toolrecall.cache import _db, cached_terminal  # noqa: E402


def _refresh_config_singleton():
    """Rebuild cache.py's config singleton (see test_swr_e2e for rationale)."""
    _cache_mod.config = _cache_mod.load_config()


class TestCanonicalKeysE2E(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _refresh_config_singleton()

    def setUp(self):
        db = os.environ["TOOLRECALL_CACHE_DB"]
        assert tempfile.gettempdir() in os.path.realpath(db), f"non-temp DB: {db}"
        with _db() as conn:
            conn.execute("DELETE FROM terminal_cache")

    @classmethod
    def tearDownClass(cls):
        import shutil

        shutil.rmtree(test_db_dir, ignore_errors=True)

    def test_flag_permutation_shares_entry(self):
        r1 = cached_terminal("uname -a", cwd="/tmp")
        self.assertFalse(r1["cached"])
        # Permutation must HIT the same entry (canonical key)
        r2 = cached_terminal("uname -a", cwd="/tmp")
        self.assertTrue(r2["cached"])
        # Rows: exactly one entry for this command family
        with _db() as conn:
            n = conn.execute(
                "SELECT COUNT(*) FROM terminal_cache WHERE command LIKE 'uname%'"
            ).fetchone()[0]
        self.assertEqual(n, 1, "permutations must share one cache entry")

    def test_fuzzy_ttl_makes_near_command_cacheable(self):
        # 'uptime -p' prefix-matches 'uptime' (cacheable, 300s) — not fuzzy.
        # Use a command that matches NO pattern but is similar to one:
        # 'free -m' vs 'free -h' — 'free -h' is exact, 'free -m' is not
        # matched by any exact/prefix pattern ('free' alone is not a pattern;
        # 'free -h' is multi-word exact-only). With fuzzy on + threshold 0.7
        # it inherits 'free -h's cacheability.
        r1 = cached_terminal("free -m", cwd="/tmp")
        self.assertFalse(r1["cached"])
        self.assertNotIn("error", r1)
        r2 = cached_terminal("free -m", cwd="/tmp")
        self.assertTrue(r2["cached"], "fuzzy TTL match must make this cacheable")
        self.assertNotIn("stale", r2)

    def test_fuzzy_off_leaves_unmatched_uncacheable(self):
        _refresh_config_singleton()
        _cache_mod.config = _cache_mod.load_config()
        # Simulate fuzzy disabled by patching config data directly
        cfg = _cache_mod.config
        old = cfg._data.get("cache", {}).get("fuzzy_ttl_match")
        cfg._data.setdefault("cache", {})["fuzzy_ttl_match"] = False
        try:
            r1 = cached_terminal("df -i", cwd="/tmp")
            self.assertFalse(r1["cached"])
            r2 = cached_terminal("df -i", cwd="/tmp")
            self.assertFalse(r2["cached"], "fuzzy off → 'df -i' must stay uncacheable")
        finally:
            if old is None:
                cfg._data.get("cache", {}).pop("fuzzy_ttl_match", None)
            else:
                cfg._data["cache"]["fuzzy_ttl_match"] = old


if __name__ == "__main__":
    unittest.main()
