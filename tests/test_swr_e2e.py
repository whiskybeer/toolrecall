"""E2E tests for stale-while-revalidate + adaptive TTL in terminal/MCP/API caches.

Isolated DB + env-driven config. Env vars are set at module import (before
toolrecall is imported); per-test isolation clears the cache tables through
the same ``_db()`` singleton — deleting the DB file under the open WAL
connection would leave stale data in the old inode.
Uses 'hostname' — a DEFAULT_CACHEABLE allowlisted command (explicit ttl does
NOT make a non-allowlisted command cacheable; the allowlist decides).
"""

import os
import sys
import tempfile
import time
import unittest

test_db_dir = tempfile.mkdtemp()
test_db_path = os.path.join(test_db_dir, "test_swr.db")
os.environ["TOOLRECALL_CACHE_DB"] = test_db_path
os.environ["TOOLRECALL_STALE_WHILE_REVALIDATE"] = "60"
os.environ["TOOLRECALL_ADAPTIVE_TTL"] = "true"
os.environ["TOOLRECALL_ADAPTIVE_FACTOR"] = "2.0"
os.environ["TOOLRECALL_ADAPTIVE_MAX_TTL"] = "3600"

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from toolrecall.cache import (  # noqa: E402
    cached_terminal,
    cached_mcp_check,
    cached_mcp_store,
    cached_mcp,
    cached_api_check,
    cached_api_store,
    _swr_window,
    _adaptive_cfg,
    _db,
    _hash,
)

CMD = "hostname"

import toolrecall.cache as _cache_mod  # noqa: E402


def _refresh_config_singleton():
    """Rebuild toolrecall.cache's module-level config singleton.

    pytest imports test modules alphabetically: test_docs_*.py imports
    toolrecall (via toolrecall.docs) BEFORE this module's env-var lines run,
    so cache.py's ``config = load_config()`` singleton was built without
    TOOLRECALL_STALE_WHILE_REVALIDATE / TOOLRECALL_ADAPTIVE_* set. Rebuilding
    it in setUpClass makes _swr_window()/_adaptive_cfg() see the intended
    values regardless of collection order.
    """
    _cache_mod.config = _cache_mod.load_config()


class TestSwrTerminal(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _refresh_config_singleton()

    def setUp(self):
        # Safety invariant for the bare DELETE below: TOOLRECALL_CACHE_DB
        # points at a THROWAWAY temp DB (this module's pin, the conftest
        # hermetic.db, or another test module's temp pin — pytest may import
        # several modules that each pin their own). Never the user's real
        # cache. Assert so the intent is machine-checkable (static scanners
        # flag WHERE-less DELETEs).
        db = os.environ["TOOLRECALL_CACHE_DB"]
        assert tempfile.gettempdir() in os.path.realpath(db), f"refusing to clear non-temp DB: {db}"
        with _db() as conn:
            conn.execute("DELETE FROM terminal_cache")

    @classmethod
    def tearDownClass(cls):
        import shutil

        shutil.rmtree(test_db_dir, ignore_errors=True)

    def _backdate(self, command: str, seconds: int):
        with _db() as conn:
            conn.execute(
                "UPDATE terminal_cache SET expires_at = expires_at - ? WHERE command = ?",
                (seconds, command),
            )

    def _row(self, command: str):
        with _db() as conn:
            return conn.execute(
                "SELECT output, expires_at, hit_streak FROM terminal_cache WHERE command = ?",
                (command,),
            ).fetchone()

    def test_config_env_overrides_active(self):
        self.assertEqual(_swr_window(), 60)
        enabled, factor, cap = _adaptive_cfg()
        self.assertTrue(enabled)
        self.assertEqual(factor, 2.0)
        self.assertEqual(cap, 3600.0)

    def test_fresh_hit_increments_streak(self):
        r1 = cached_terminal(CMD, ttl=30, cwd="/tmp")
        self.assertFalse(r1["cached"])
        r2 = cached_terminal(CMD, ttl=30, cwd="/tmp")
        self.assertTrue(r2["cached"])
        self.assertNotIn("stale", r2)
        row = self._row(CMD)
        self.assertGreaterEqual(row[2], 1)  # hit_streak grew

    def test_stale_hit_serves_and_revalidates(self):
        r1 = cached_terminal(CMD, ttl=30, cwd="/tmp")
        self.assertFalse(r1["cached"])
        # Push expires_at into the past, within the 60s SWR window
        self._backdate(CMD, 30 + 5)
        r2 = cached_terminal(CMD, ttl=30, cwd="/tmp")
        self.assertTrue(r2["cached"])
        self.assertTrue(r2.get("stale"), "stale-window hit must be flagged stale=True")
        # Wait for the background revalidate thread, then verify refresh
        deadline = time.time() + 5
        refreshed = False
        while time.time() < deadline:
            row = self._row(CMD)
            if row and row[1] > time.time() + 20:  # expires_at pushed back out
                refreshed = True
                break
            time.sleep(0.05)
        self.assertTrue(refreshed, "background revalidate did not refresh the row")
        # Next call should be fresh again (no stale flag)
        r3 = cached_terminal(CMD, ttl=30, cwd="/tmp")
        self.assertTrue(r3["cached"])
        self.assertNotIn("stale", r3)

    def test_expired_beyond_swr_window_is_a_miss(self):
        cached_terminal(CMD, ttl=30, cwd="/tmp")
        # Backdate past both TTL and the 60s SWR window
        self._backdate(CMD, 120)
        r2 = cached_terminal(CMD, ttl=30, cwd="/tmp")
        self.assertFalse(r2["cached"], "expired past SWR window must re-execute")
        self.assertNotIn("stale", r2)

    def test_adaptive_ttl_stretches_on_revalidate(self):
        cached_terminal(CMD, ttl=10, cwd="/tmp")
        for _ in range(3):
            r = cached_terminal(CMD, ttl=10, cwd="/tmp")
            self.assertTrue(r["cached"])
        row = self._row(CMD)
        self.assertGreaterEqual(row[2], 3, "hit_streak should track fresh hits")
        self._backdate(CMD, 10 + 5)
        r = cached_terminal(CMD, ttl=10, cwd="/tmp")
        self.assertTrue(r.get("stale"))
        deadline = time.time() + 5
        stretched = False
        while time.time() < deadline:
            row = self._row(CMD)
            if row and row[1] > time.time() + 15:  # > base ttl 10 → adaptive stretch
                stretched = True
                break
            time.sleep(0.05)
        self.assertTrue(stretched, "adaptive TTL did not stretch past base ttl")


class TestSwrMcp(unittest.TestCase):
    ARGS = {"a": 1}

    @classmethod
    def setUpClass(cls):
        _refresh_config_singleton()

    def _key(self):
        import json as _json

        return _hash(f"srv://tool?{_json.dumps(self.ARGS, sort_keys=True)}")

    def setUp(self):
        # See TestSwrTerminal.setUp: temp-DB invariant, asserted per class.
        db = os.environ["TOOLRECALL_CACHE_DB"]
        assert tempfile.gettempdir() in os.path.realpath(db), f"refusing to clear non-temp DB: {db}"
        with _db() as conn:
            conn.execute("DELETE FROM mcp_cache")

    def _backdate(self, request_hash: str, seconds: int):
        with _db() as conn:
            conn.execute(
                "UPDATE mcp_cache SET expires_at = expires_at - ? WHERE request_hash = ?",
                (seconds, request_hash),
            )

    def test_fresh_hit_and_streak(self):
        k = self._key()
        cached_mcp_store(k, "srv", "tool", self.ARGS, '"v1"', ttl=30)
        r = cached_mcp_check("srv", "tool", self.ARGS, ttl=30)
        self.assertTrue(r["cached"])
        self.assertNotIn("stale", r)
        with _db() as conn:
            row = conn.execute(
                "SELECT hit_streak FROM mcp_cache WHERE request_hash = ?", (k,)
            ).fetchone()
        self.assertGreaterEqual(row[0], 1)

    def test_stale_served_with_flag(self):
        k = self._key()
        cached_mcp_store(k, "srv", "tool", self.ARGS, '"v1"', ttl=30)
        self._backdate(k, 35)
        r = cached_mcp_check("srv", "tool", self.ARGS, ttl=30)
        self.assertTrue(r["cached"])
        self.assertTrue(r.get("stale"))
        self.assertEqual(r["key"], k)

    def test_one_shot_refetches_stale_via_fetch_fn(self):
        calls = []
        k = self._key()
        cached_mcp_store(k, "srv", "tool", self.ARGS, '"stale-v"', ttl=30)
        self._backdate(k, 35)

        def fetch():
            calls.append(1)
            return "fresh-v"

        out = cached_mcp("srv", "tool", self.ARGS, fetch_fn=fetch, ttl=30)
        self.assertEqual(out, "stale-v")  # stale data served immediately
        self.assertEqual(len(calls), 1)  # and refetched exactly once
        r = cached_mcp_check("srv", "tool", self.ARGS, ttl=30)
        self.assertTrue(r["cached"])
        self.assertNotIn("stale", r)  # row is fresh again
        with _db() as conn:
            row = conn.execute("SELECT data FROM mcp_cache WHERE request_hash = ?", (k,)).fetchone()
        self.assertEqual(row[0], '"fresh-v"')

    def test_expired_is_miss(self):
        cached_mcp_store(self._key(), "srv", "tool", self.ARGS, '"v1"', ttl=30)
        self._backdate(self._key(), 120)
        r = cached_mcp_check("srv", "tool", self.ARGS, ttl=30)
        self.assertFalse(r["cached"])


class TestSwrApi(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _refresh_config_singleton()

    def setUp(self):
        # See TestSwrTerminal.setUp: temp-DB invariant, asserted per class.
        db = os.environ["TOOLRECALL_CACHE_DB"]
        assert tempfile.gettempdir() in os.path.realpath(db), f"refusing to clear non-temp DB: {db}"
        with _db() as conn:
            conn.execute("DELETE FROM api_cache")

    HDRS = {"content-type": "application/json"}

    def _store(self, h, body, ttl=30):
        cached_api_store(h, "POST", "api.test", "/v1/x", "bh", 200, self.HDRS, body, ttl=ttl)

    def _backdate(self, h, seconds):
        with _db() as conn:
            conn.execute(
                "UPDATE api_cache SET expires_at = expires_at - ? WHERE request_hash = ?",
                (seconds, h),
            )

    def test_fresh_hit(self):
        self._store("h-api1", '{"ok":true}')
        r = cached_api_check("h-api1")
        self.assertTrue(r["cached"])
        self.assertNotIn("stale", r)

    def test_stale_served_flagged_no_replay(self):
        self._store("h-api2", '{"ok":true}')
        self._backdate("h-api2", 35)
        r = cached_api_check("h-api2")
        self.assertTrue(r["cached"])
        self.assertTrue(r.get("stale"), "API SWR must serve stale, flagged")
        # And it must NOT auto-refresh the row (no LLM replay)
        with _db() as conn:
            row = conn.execute(
                "SELECT expires_at FROM api_cache WHERE request_hash = 'h-api2'"
            ).fetchone()
        self.assertLess(row[0], __import__("time").time(), "no background replay allowed")

    def test_expired_is_miss(self):
        self._store("h-api3", '{"ok":true}')
        self._backdate("h-api3", 120)
        self.assertFalse(cached_api_check("h-api3")["cached"])


if __name__ == "__main__":
    unittest.main()
