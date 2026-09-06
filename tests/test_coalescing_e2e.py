"""E2E tests for request coalescing on the terminal cache miss path.

Opt-in via [resilience] coalescing — N concurrent identical misses share
ONE subprocess execution.
"""

import os
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest import mock

test_db_dir = tempfile.mkdtemp()
test_db_path = os.path.join(test_db_dir, "test_coalesce.db")
os.environ["TOOLRECALL_CACHE_DB"] = test_db_path
os.environ["TOOLRECALL_RESILIENCE_COALESCING"] = "true"

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import toolrecall.cache as _cache_mod  # noqa: E402
from toolrecall.cache import _db, cached_terminal  # noqa: E402


def _refresh_config_singleton():
    _cache_mod.config = _cache_mod.load_config()


class TestCoalescingE2E(unittest.TestCase):
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

    def test_concurrent_misses_execute_once(self):
        # Non-allowlisted command would not be cacheable — use 'hostname'
        # with a marker via cwd trick isn't possible; instead track executor
        # invocations directly.
        calls = []
        real = _cache_mod._run_terminal_subprocess

        def counting(cmd, exec_cwd):
            calls.append(1)
            time.sleep(0.15)  # widen the race window
            return real(cmd, exec_cwd)

        with mock.patch.object(_cache_mod, "_run_terminal_subprocess", side_effect=counting):
            with ThreadPoolExecutor(max_workers=8) as pool:
                futures = [pool.submit(cached_terminal, "hostname", None, "/tmp") for _ in range(8)]
                results = [f.result() for f in futures]

        self.assertEqual(len(calls), 1, f"producer must run once, ran {len(calls)}x")
        for r in results:
            self.assertTrue(r.get("cached") or r.get("output"))

    def test_coalescing_off_runs_normally(self):
        cfg = _cache_mod.config
        old = cfg._data.get("resilience")
        cfg._data["resilience"] = {"coalescing": False}
        try:
            r1 = cached_terminal("hostname", None, "/tmp")
            self.assertFalse(r1["cached"])
            r2 = cached_terminal("hostname", None, "/tmp")
            self.assertTrue(r2["cached"])
        finally:
            if old is None:
                cfg._data.pop("resilience", None)
            else:
                cfg._data["resilience"] = old


if __name__ == "__main__":
    unittest.main()
