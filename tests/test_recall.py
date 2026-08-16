"""Tests for the recall tier.

Covers the opt-in config gate (default OFF) plus the config property.
deps-free, no network — mirrors the test_turso_optin gating pattern.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from toolrecall import _db  # noqa: E402
from toolrecall.config import load_config  # noqa: E402


class TestRecallConfigGate(unittest.TestCase):
    """The recall tier must be OFF unless explicitly enabled."""

    def setUp(self):
        self._orig = dict(os.environ)
        _db._cached_config = None

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._orig)
        _db._cached_config = None

    def test_recall_disabled_by_default(self):
        cfg = load_config()
        self.assertFalse(cfg.recall_enabled)

    def test_recall_enabled_env_coercion(self):
        from toolrecall.config import load_config

        for raw, expected in [
            ("true", True),
            ("1", True),
            ("yes", True),
            ("false", False),
            ("0", False),
            ("no", False),
            ("", False),
        ]:
            os.environ["TOOLRECALL_RECALL_ENABLED"] = raw
            cfg = load_config()
            self.assertEqual(cfg.recall_enabled, expected, f"raw={raw!r}")
        os.environ.pop("TOOLRECALL_RECALL_ENABLED")

    def test_recall_absent_key_defaults_off(self):
        # Even if some unrelated config is present, absent [recall] == off.
        cfg = load_config()
        self.assertFalse(cfg.get("recall", "enabled", default=False))


class TestRecallSchema(unittest.TestCase):
    """recall_cache table must exist with the expected columns after _init()."""

    def setUp(self):
        import tempfile

        self._tmp = tempfile.mkdtemp()
        self._db_path = os.path.join(self._tmp, "test_recall_cache.db")
        os.environ["TOOLRECALL_CACHE_DB"] = self._db_path
        from toolrecall._db import _db_lock, _db_real
        import toolrecall._db as _db_mod

        _db_lock.acquire()
        if _db_real is not None:
            _db_real.close()
            _db_mod._db_real = None
        _db_lock.release()
        _db._cached_config = None
        from toolrecall.cache import _init

        _init()

    def tearDown(self):
        import shutil

        os.environ.pop("TOOLRECALL_CACHE_DB", None)
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _columns(self, table):
        from toolrecall._db import _db as _db_call

        with _db_call() as conn:
            rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        return {r[1] for r in rows}

    def test_recall_cache_table_exists_with_expected_columns(self):
        cols = self._columns("recall_cache")
        expected = {
            "node_id",
            "fingerprint",
            "content",
            "content_type",
            "reproducible",
            "summary",
            "tokens",
            "cached_at",
        }
        self.assertTrue(expected.issubset(cols), f"missing: {expected - cols}")

    def test_recall_cache_node_id_is_primary_key(self):
        from toolrecall._db import _db as _db_call

        with _db_call() as conn:
            pk = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='recall_cache'"
            ).fetchone()
        self.assertIsNotNone(pk)


if __name__ == "__main__":
    unittest.main()
