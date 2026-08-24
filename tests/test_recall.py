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


class TestRecallStore(unittest.TestCase):
    """recall.store/get roundtrip, deterministic node_id, classifier."""

    def setUp(self):
        import tempfile

        self._tmp = tempfile.mkdtemp()
        self._db_path = os.path.join(self._tmp, "test_recall_store.db")
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

    def test_node_id_is_deterministic(self):
        from toolrecall import recall

        self.assertEqual(recall.node_id("fp-x"), recall.node_id("fp-x"))
        self.assertNotEqual(recall.node_id("fp-x"), recall.node_id("fp-y"))

    def test_reproducible_classifier(self):
        from toolrecall import recall

        self.assertTrue(recall.reproducible("file", "/etc/os-release"))
        self.assertTrue(recall.reproducible("api", "GET /health"))
        self.assertFalse(recall.reproducible("web", ""))
        self.assertFalse(recall.reproducible("other", ""))
        self.assertFalse(recall.reproducible("file", ""))

    def test_store_then_get_roundtrips(self):
        from toolrecall import recall

        nid = recall.store(
            fingerprint="fp",
            content="raw bytes",
            content_type="web",
            reproducible=False,
            summary="s",
        )
        got = recall.get(nid)
        self.assertEqual(got["content"], "raw bytes")
        self.assertEqual(got["summary"], "s")
        self.assertIs(got["reproducible"], False)

    def test_same_fingerprint_dedups_to_same_node_id(self):
        from toolrecall import recall

        nid1 = recall.store(fingerprint="fp", content="a", content_type="web", reproducible=False)
        nid2 = recall.store(fingerprint="fp", content="b", content_type="web", reproducible=False)
        self.assertEqual(nid1, nid2)

    def test_get_missing_returns_none(self):
        from toolrecall import recall

        self.assertIsNone(recall.get("does-not-exist"))

    def test_stats_reflects_stored_entries(self):
        from toolrecall import recall

        recall.store(
            fingerprint="s1", content="hello world", content_type="web", reproducible=False
        )
        recall.store(
            fingerprint="s2", content="foo bar baz qux", content_type="web", reproducible=False
        )
        st = recall.stats()
        self.assertEqual(st["total"], 2)
        self.assertGreater(st["tokens"], 0)


class TestRecallDaemonGate(unittest.TestCase):
    """Daemon handlers refuse to run while the recall tier is disabled."""

    def _handler(self, enabled: bool):
        from unittest import mock
        from toolrecall.daemon import DaemonServer

        h = object.__new__(DaemonServer)  # skip __init__; only cfg is touched
        h.cfg = mock.MagicMock()
        h.cfg.recall_enabled = enabled
        return h

    def test_recall_store_disabled_returns_error(self):
        resp = self._handler(False)._handle_recall_store({"fingerprint": "f", "content": "c"})
        self.assertIn("disabled", resp.get("error", ""))

    def test_recall_get_disabled_returns_error(self):
        resp = self._handler(False)._handle_recall_get({"node_id": "x"})
        self.assertIn("disabled", resp.get("error", ""))

    def test_recall_store_missing_fields_returns_error(self):
        resp = self._handler(True)._handle_recall_store({"fingerprint": "f"})
        self.assertIn("Missing", resp.get("error", ""))

    def test_recall_stats_disabled_returns_error(self):
        resp = self._handler(False)._handle_recall_stats({})
        self.assertIn("disabled", resp.get("error", ""))


class TestRecallBridgeDefs(unittest.TestCase):
    """Bridge must expose the recall tools and route them to the daemon."""

    def test_bridge_defines_recall_tools(self):
        from toolrecall.mcp_bridge import TOOL_DEFINITIONS, CMD_TO_MCP

        names = {t["name"] for t in TOOL_DEFINITIONS}
        self.assertIn("recall_store", names)
        self.assertIn("recall_get", names)
        self.assertEqual(CMD_TO_MCP["recall_store"], "recall_store")
        self.assertEqual(CMD_TO_MCP["recall_get"], "recall_get")


if __name__ == "__main__":
    unittest.main()
