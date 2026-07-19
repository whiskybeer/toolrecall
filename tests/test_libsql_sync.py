"""Unit tests for the libsql-sync backend (toolrecall.storage.libsql_sync).

These tests verify the sync gating logic, stats_info, import handling,
and error messages -- WITHOUT requiring a running sqld or Turso Cloud.
They use pyturso's test-time import guard so the tests pass even if
pyturso is not installed.
"""

import os
import sys
import unittest
from unittest import mock


MODPATH = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if MODPATH not in sys.path:
    sys.path.insert(0, MODPATH)


class TestLibSQLSyncBackend(unittest.TestCase):
    """Tests for the libsql_sync backend module."""

    def setUp(self):
        # Isolate env
        self._env = {}
        for k in ["TOOLRECALL_STORAGE_BACKEND", "TOOLRECALL_SYNC_ENABLED",
                   "TOOLRECALL_SYNC_URL", "TOOLRECALL_SYNC_TOKEN",
                   "TOOLRECALL_LIBSQL_DB_PATH"]:
            self._env[k] = os.environ.pop(k, None)
        self._tmp_db = "/tmp/test_libsql_sync.test"

    def tearDown(self):
        for k, v in self._env.items():
            if v is not None:
                os.environ[k] = v
            else:
                os.environ.pop(k, None)
        for p in [self._tmp_db, self._tmp_db + "-wal", self._tmp_db + "-shm"]:
            try:
                os.remove(p)
            except FileNotFoundError:
                pass

    def test_01_sync_configured_disabled_by_default(self):
        """sync_configured() returns False when nothing is configured."""
        from toolrecall.storage.libsql_sync import sync_configured
        from toolrecall.config import load_config
        cfg = load_config()
        self.assertFalse(sync_configured(cfg))

    def test_02_sync_configured_requires_all_three(self):
        """sync_configured() requires sync_enabled + sync_url + sync_token."""
        from toolrecall.storage.libsql_sync import sync_configured
        from toolrecall.config import load_config

        # Only sync_enabled
        os.environ["TOOLRECALL_SYNC_ENABLED"] = "true"
        cfg = load_config()
        self.assertFalse(sync_configured(cfg), "sync_enabled alone is not enough")

        # Only sync_url
        os.environ["TOOLRECALL_SYNC_URL"] = "http://127.0.0.1:8567"
        cfg = load_config()
        self.assertFalse(sync_configured(cfg), "enabled + url still needs token")

        # All three
        os.environ["TOOLRECALL_SYNC_TOKEN"] = "local"
        cfg = load_config()
        self.assertTrue(sync_configured(cfg), "all three should return True")

        # Remove sync_enabled
        del os.environ["TOOLRECALL_SYNC_ENABLED"]
        cfg = load_config()
        self.assertFalse(sync_configured(cfg), "without enabled flag, should be False")

    def test_03_sync_configured_empty_token_is_false(self):
        """Empty sync_token should be treated as not configured."""
        from toolrecall.storage.libsql_sync import sync_configured
        from toolrecall.config import load_config

        os.environ["TOOLRECALL_SYNC_ENABLED"] = "true"
        os.environ["TOOLRECALL_SYNC_URL"] = "http://127.0.0.1:8567"
        os.environ["TOOLRECALL_SYNC_TOKEN"] = ""
        cfg = load_config()
        self.assertFalse(sync_configured(cfg))

    def test_04_sync_configured_empty_url_is_false(self):
        """Empty sync_url should be treated as not configured."""
        from toolrecall.storage.libsql_sync import sync_configured
        from toolrecall.config import load_config

        os.environ["TOOLRECALL_SYNC_ENABLED"] = "true"
        os.environ["TOOLRECALL_SYNC_URL"] = ""
        os.environ["TOOLRECALL_SYNC_TOKEN"] = "tok"
        cfg = load_config()
        self.assertFalse(sync_configured(cfg))

    def test_05_stats_info_keys(self):
        """stats_info() returns expected keys for libsql-sync backend."""
        from toolrecall.storage.libsql_sync import stats_info
        from toolrecall.config import load_config
        cfg = load_config()
        info = stats_info(cfg)
        self.assertIn("sync_enabled", info)
        self.assertIn("sync_url", info)
        self.assertIn("sync_interval", info)
        self.assertIn("sync_backend", info)
        self.assertEqual(info["sync_backend"], "pyturso")
        self.assertFalse(info["sync_enabled"])

    def test_06_stats_info_with_config(self):
        """stats_info() reflects configured values."""
        from toolrecall.storage.libsql_sync import stats_info
        from toolrecall.config import load_config

        os.environ["TOOLRECALL_SYNC_ENABLED"] = "true"
        os.environ["TOOLRECALL_SYNC_URL"] = "http://127.0.0.1:8567"
        os.environ["TOOLRECALL_SYNC_TOKEN"] = "tok"
        cfg = load_config()
        info = stats_info(cfg)
        self.assertTrue(info["sync_enabled"])
        self.assertEqual(info["sync_url"], "http://127.0.0.1:8567")

    def test_07_connect_raises_without_pyturso(self):
        """connect() raises ImportError when pyturso is not installed."""
        from toolrecall.storage.libsql_sync import connect
        from toolrecall.config import load_config

        os.environ["TOOLRECALL_SYNC_ENABLED"] = "true"
        os.environ["TOOLRECALL_SYNC_URL"] = "http://127.0.0.1:8567"
        os.environ["TOOLRECALL_SYNC_TOKEN"] = "tok"
        cfg = load_config()

        with mock.patch("toolrecall.storage.libsql_sync._HAS_PYTURSO", False):
            with self.assertRaises(ImportError) as ctx:
                connect(cfg, self._tmp_db)
            self.assertIn("pip install toolrecall[libsql-sync]", str(ctx.exception))

    def test_08_connect_raises_without_sync_config(self):
        """connect() raises ValueError without sync_url + sync_token."""
        from toolrecall.storage.libsql_sync import connect
        from toolrecall.config import load_config

        cfg = load_config()  # no sync config at all

        with mock.patch("toolrecall.storage.libsql_sync._HAS_PYTURSO", True):
            with self.assertRaises(ValueError) as ctx:
                connect(cfg, self._tmp_db)
            msg = str(ctx.exception)
            self.assertIn("sync_url", msg)
            self.assertIn("sync_token", msg)
            self.assertIn("backend='libsql'", msg)

    def test_09_SUPPORTS_SYNC_is_true(self):
        """SUPPORTS_SYNC is True for the sync backend."""
        from toolrecall.storage.libsql_sync import SUPPORTS_SYNC
        self.assertTrue(SUPPORTS_SYNC)

    def test_10_resolve_backend_name(self):
        """resolve_backend_name() recognises libsql-sync."""
        from toolrecall.storage import resolve_backend_name
        from toolrecall.config import load_config

        os.environ["TOOLRECALL_STORAGE_BACKEND"] = "libsql-sync"
        cfg = load_config()
        self.assertEqual(resolve_backend_name(cfg), "libsql-sync")

    def test_11_active_db_path_for_libsql_sync(self):
        """active_db_path() returns the libsql-specific path for libsql-sync."""
        from toolrecall.storage import active_db_path
        from toolrecall.config import load_config

        os.environ["TOOLRECALL_STORAGE_BACKEND"] = "libsql-sync"
        cfg = load_config()
        path = active_db_path(cfg)
        self.assertTrue(path.endswith("cache-libsql.db"),
                        f"Expected cache-libsql.db, got {path}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
