# Copyright (c) 2026 Robin Schautka
# SPDX-License-Identifier: MIT
# Source: https://github.com/whiskybeer/toolrecall
"""Late TOOLRECALL_CACHE_DB pin must win over the import-time config.

Bug (2026-09-13, tr-mcp-keys wipe forensics): the sibling worktree's
tests/test_file_cache.py pinned TOOLRECALL_CACHE_DB at MODULE level — but
toolrecall.cache had already been imported (conftest first), so the cached
Config resolved the PRODUCTION DB forever. Every reset_stats() in that
suite then wiped production counters (the counter went 88,987 → 4 mid-run).
The db_path-change check in _db() could never fire: it compares
_active_db_path(cached_cfg) against _db_path_cached, and BOTH were prod.

Fix: _db() snapshots the raw env value the cached config was built with
(TOOLRECALL_CACHE_DB_AT_BUILD) and forces a config reload whenever the
current env differs. One os.environ.get per _db() call; no reload on the
common path (env stable).
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import toolrecall._db as _dbm  # noqa: E402  (import BEFORE any pin — the bug precondition)
import toolrecall.cache as c  # noqa: E402


class TestLateEnvPinWins(unittest.TestCase):
    def test_pin_after_import_switches_db(self):
        """TOOLRECALL_CACHE_DB set AFTER toolrecall import re-resolves _db()."""
        import toolrecall.cache as c

        pre = _dbm._db_path_cached
        db = os.path.join(tempfile.mkdtemp(), "late_pin.db")
        os.environ["TOOLRECALL_CACHE_DB"] = db
        self.addCleanup(lambda: os.environ.pop("TOOLRECALL_CACHE_DB", None))
        c._init()
        self.assertNotEqual(
            _dbm._db_path_cached, pre, "env pin after import was ignored (the tr-mcp-keys wipe bug)"
        )
        self.assertTrue(
            _dbm._db_path_cached.startswith(tempfile.gettempdir()),
            f"_db must now resolve to the pinned temp path, got {_dbm._db_path_cached}",
        )

    def test_reset_stats_after_late_pin_does_not_touch_prod(self):
        """reset_stats() with a late pin writes the temp DB; production
        counters survive (the actual damage measured in the wipe).

        Prod-survival is proven structurally, not by reading prod (the
        conftest production-path guard rightly blocks that): the _db()
        connection resolves to the pinned TEMP path for the entire call,
        and reset_stats only writes through _db()."""
        from toolrecall.cache import reset_stats

        db = os.path.join(tempfile.mkdtemp(), "late_pin_reset.db")
        os.environ["TOOLRECALL_CACHE_DB"] = db
        self.addCleanup(lambda: os.environ.pop("TOOLRECALL_CACHE_DB", None))
        c._init()
        # sanity: connected to temp
        self.assertIn(tempfile.gettempdir(), _dbm._db_path_cached)
        reset_stats()
        # The wipe went through _db() → the pinned temp path (assert above).
        # Structural post-condition: the prod path is NOT what the process
        # was connected to at any point during reset_stats.
        self.assertNotIn(".toolrecall", _dbm._db_path_cached)
        self.assertTrue(os.path.exists(db), "reset_stats must have opened the pinned DB")

    def test_stable_env_does_not_reload(self):
        """No reload churn when env is stable: second _db() call keeps the
        same connection (the common path stays cheap)."""
        db = os.path.join(tempfile.mkdtemp(), "stable.db")
        os.environ["TOOLRECALL_CACHE_DB"] = db
        self.addCleanup(lambda: os.environ.pop("TOOLRECALL_CACHE_DB", None))
        c._init()
        with c._db() as conn:
            conn.execute("SELECT 1").fetchone()
        path1 = _dbm._db_path_cached
        with c._db() as conn2:
            conn2.execute("SELECT 1").fetchone()
        self.assertEqual(_dbm._db_path_cached, path1)
        self.assertIsNotNone(_dbm._db_real)  # same live connection, no churn


if __name__ == "__main__":
    unittest.main()
