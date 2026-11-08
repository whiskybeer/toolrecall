"""scope-aware cache_invalidate: agent scope keeps api_cache (billed LLM responses).

`invalidate_all()` has never cleared api_cache — deliberate (wiping billed
responses re-bills every replayed call; in-flight proxy stores re-insert
immediately), but it was only documented in a test comment while the reply
string claimed "All ToolRecall caches cleared". This pins the contract in
executable form:

- default (scope="agent"): agent caches cleared, api_cache untouched —
  honest reply string states the exception.
- scope="api": explicit opt-in — agent caches + whole api_cache table,
  with the deleted api row count returned (money-tier visibility).
- unknown scope values: rejected BEFORE any deletion; the security gate
  still runs first.
- direct API: invalidate_api_cache() deletes only api_cache and returns
  the rowcount.
- client fallback (daemon down): scope="agent" leaves api_cache alone;
  scope="api" wipes it too — the scope survives the daemon-down path.
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from toolrecall._db import _db
from toolrecall.cache import (
    cached_api_store,
    invalidate_all,
    invalidate_api_cache,
)

# ── Hermetic temp-DB pin (pattern: tests/test_daemon_error_honesty.py) ──

_TEMP_DB_DIR = tempfile.mkdtemp(prefix="tr_invalidate_scope_")
_TEMP_DB_PATH = os.path.join(_TEMP_DB_DIR, "invalidate_scope.db")
os.environ["TOOLRECALL_CACHE_DB"] = _TEMP_DB_PATH

import toolrecall.cache as _cache_mod  # noqa: E402
from toolrecall.config import load_config  # noqa: E402

_cache_mod.config = load_config()
import toolrecall._db as _db_mod  # noqa: E402

if _db_mod._db_real is not None:
    _db_mod._db_real.close()
    _db_mod._db_real = None
_db_mod._db_path_cached = None
_cache_mod._init()

_API_HEADERS = {"content-type": "application/json"}


def _seed_api_row(request_hash: str) -> None:
    cached_api_store(
        request_hash,
        "POST",
        "api.test",
        "/v1/x",
        "bh-" + request_hash,
        200,
        _API_HEADERS,
        b'{"ok": true}',
        ttl=300,
    )


def _count(table: str) -> int:
    with _db() as conn:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


class _TempDbSafety:
    """Assert the module pin points at a throwaway temp DB before DELETEs."""

    @staticmethod
    def check():
        db = os.environ["TOOLRECALL_CACHE_DB"]
        assert tempfile.gettempdir() in os.path.realpath(db), f"refusing to clear non-temp DB: {db}"
        # conftest force-pins a FRESH per-test DB (see _isolated_toolrecall_env);
        # this file used to rely on its module-level env write keeping an
        # already-initialized shared DB — bootstrap idempotent schema explicitly.
        _cache_mod._init()


class TestDirectCacheScopes(unittest.TestCase):
    """invalidate_all vs invalidate_api_cache table-level contract."""

    def setUp(self):
        _TempDbSafety.check()
        with _db() as conn:
            conn.execute("DELETE FROM api_cache")
            conn.execute("DELETE FROM file_cache")
        _seed_api_row("h-inv-1")
        with _db() as conn:
            conn.execute(
                "INSERT INTO file_cache (path_hash, path, content, mtime, "
                "size, cached_at) VALUES ('ph-inv-1', '/tmp/x.py', 'x', 1, 1, 1)"
            )

    def test_seed_roundtrip(self):
        """Seed sanity: the api row is checkable before any wipe runs."""
        from toolrecall.cache import cached_api_check

        res = cached_api_check("h-inv-1")
        self.assertTrue(res.get("cached"), f"seed row not checkable: {res}")

    def test_invalidate_all_keeps_api_cache(self):
        invalidate_all()
        self.assertEqual(_count("api_cache"), 1, "invalidate_all must not touch api_cache")
        self.assertEqual(_count("file_cache"), 0, "invalidate_all must clear file_cache")

    def test_invalidate_api_cache_only_api(self):
        rows = invalidate_api_cache()
        self.assertEqual(rows, 1)
        self.assertEqual(_count("api_cache"), 0)
        self.assertEqual(_count("file_cache"), 1, "invalidate_api_cache must not touch file_cache")


class _DaemonHandlerHarness:
    """Bind DaemonServer._handle_invalidate to a stub self (no socket/DB)."""

    @staticmethod
    def call(req: dict, gate_err: str | None = None) -> dict:
        from unittest.mock import MagicMock

        from toolrecall.daemon import DaemonServer

        daemon = MagicMock(spec=DaemonServer)
        daemon.security = MagicMock()
        daemon.security.check_invalidate.return_value = gate_err
        return DaemonServer._handle_invalidate(daemon, req)


class TestDaemonHandlerScopes(unittest.TestCase):
    """_handle_invalidate scope handling (security gate + scope validation)."""

    def setUp(self):
        _TempDbSafety.check()
        with _db() as conn:
            conn.execute("DELETE FROM api_cache")
        _seed_api_row("h-inv-2")

    def test_agent_scope_reply_is_honest(self):
        resp = _DaemonHandlerHarness.call({"scope": "agent"})
        self.assertIn("api_cache untouched", resp["result"])
        # The reply names the opt-in remedy — but never claims api was wiped.
        self.assertIn("scope=api", resp["result"])
        self.assertNotIn("caches cleared (scope=api)", resp["result"])

    def test_api_scope_reports_rowcount(self):
        resp = _DaemonHandlerHarness.call({"scope": "api"})
        self.assertIn("api_cache entries", resp["result"])
        self.assertEqual(resp["api_entries_cleared"], 1)

    def test_unknown_scope_rejected_before_deletion(self):
        _seed_api_row("h-inv-2b")
        resp = _DaemonHandlerHarness.call({"scope": "bogus"})
        self.assertIn("error", resp)
        self.assertIn("unknown scope", resp["error"])
        self.assertEqual(_count("api_cache"), 2, "no deletion on unknown scope")

    def test_gate_blocks_before_scope_check(self):
        resp = _DaemonHandlerHarness.call({"scope": "api"}, gate_err="disabled")
        self.assertEqual(resp, {"error": "disabled"})
        self.assertEqual(_count("api_cache"), 1, "gate must run before any deletion")


class TestClientFallbackScopes(unittest.TestCase):
    """cache_invalidate(scope=...) when the daemon is down.

    Isolation invariant from tests/test_client.py: patch the transport to a
    dead temp socket so the prod daemon's socket is never touched.
    """

    def setUp(self):
        _TempDbSafety.check()
        self.tmpdir = tempfile.mkdtemp()
        dead_sock = os.path.join(self.tmpdir, "dead.sock")
        import toolrecall.client as cl

        cl.set_socket_path(dead_sock)
        self._cl = cl

    def tearDown(self):
        self._cl._client = None

    def _reset_tables(self):
        with _db() as conn:
            conn.execute("DELETE FROM api_cache")
            conn.execute("DELETE FROM file_cache")

    def test_fallback_agent_keeps_api(self):
        self._reset_tables()
        _seed_api_row("h-inv-3")
        out = self._cl.cache_invalidate()
        self.assertIn("api_cache untouched", out)
        self.assertEqual(_count("api_cache"), 1, "daemon-down agent fallback must keep api_cache")
        self.assertEqual(_count("file_cache"), 0)

    def test_fallback_api_scope_wipes(self):
        self._reset_tables()
        _seed_api_row("h-inv-4")
        out = self._cl.cache_invalidate(scope="api")
        self.assertIn("api_cache entries", out)
        self.assertEqual(_count("api_cache"), 0)


if __name__ == "__main__":
    unittest.main()
