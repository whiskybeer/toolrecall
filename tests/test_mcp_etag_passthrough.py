"""Regression tests: MCP cache etag passthrough over the daemon IPC surface.

Found by the 2026-09-20 Phase C cache-correctness bench (mcp-replay-bench):

Gap — ``_handle_mcp_store`` called ``cached_mcp_store(...)`` WITHOUT the
``etag=`` parameter, so the validator column was unreachable over IPC. A
gateway built on the daemon socket could store rows but never revalidate
them, leaving clock-based freshness as its only option. Measured cost of
clock-only freshness on a mutating origin: ~29% wrong deliveries vs 0% for
validate-before-serve.

Fix — ``_handle_mcp_store`` accepts ``etag`` (and ``ttl``) and passes them
through; ``cached_mcp_check`` returns the stored ``etag`` on every hit
(fresh and stale) so the caller can build the conditional request.

These tests pin the full IPC round trip: store WITH etag -> check returns
that etag -> validation is possible.
"""

import subprocess
import sys

from tests.e2e_helpers import E2EDaemon


class TestEtagPassthroughUnit:
    """In-process: cache.py store/check carry the validator."""

    def test_store_and_check_round_trip_etag(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TOOLRECALL_CACHE_DB", str(tmp_path / "t.db"))
        import importlib

        import toolrecall.cache as cache_mod
        import toolrecall._db as db_mod

        importlib.reload(db_mod)
        importlib.reload(cache_mod)

        args = {"path": "a.py"}
        miss = cache_mod.cached_mcp_check("srv", "read", args)
        assert miss["cached"] is False

        cache_mod.cached_mcp_store(miss["key"], "srv", "read", args, "body-v1", etag='"abc123"')

        hit = cache_mod.cached_mcp_check("srv", "read", args)
        assert hit["cached"] is True
        assert hit["data"] == "body-v1"
        # the validator must come back with the hit, or no conditional
        # request can be built by the caller
        assert hit["etag"] == '"abc123"'

    def test_store_without_etag_yields_none(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TOOLRECALL_CACHE_DB", str(tmp_path / "t2.db"))
        import importlib

        import toolrecall.cache as cache_mod
        import toolrecall._db as db_mod

        importlib.reload(db_mod)
        importlib.reload(cache_mod)

        args = {"path": "b.py"}
        miss = cache_mod.cached_mcp_check("srv", "read", args)
        cache_mod.cached_mcp_store(miss["key"], "srv", "read", args, "body")

        hit = cache_mod.cached_mcp_check("srv", "read", args)
        assert hit["cached"] is True
        # explicit None (not absent) — callers branch on this to refuse
        # sharing un-validatable rows
        assert hit["etag"] is None

    def test_get_mcp_etag_matches_check(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TOOLRECALL_CACHE_DB", str(tmp_path / "t3.db"))
        import importlib

        import toolrecall.cache as cache_mod
        import toolrecall._db as db_mod

        importlib.reload(db_mod)
        importlib.reload(cache_mod)

        args = {"path": "c.py"}
        miss = cache_mod.cached_mcp_check("srv", "read", args)
        cache_mod.cached_mcp_store(miss["key"], "srv", "read", args, "body", etag='W/"v9"')

        assert cache_mod.get_mcp_etag("srv", "read", args) == 'W/"v9"'
        assert cache_mod.cached_mcp_check("srv", "read", args)["etag"] == 'W/"v9"'


class TestEtagPassthroughIPC:
    """End-to-end through a real daemon over the socket."""

    def test_ipc_store_with_etag_is_readable_by_check(self):
        with E2EDaemon() as d:
            key = "phase-c-key-1"
            r_store = d.client.send(
                {
                    "cmd": "cached_mcp_store",
                    "key": key,
                    "content": "origin-body",
                    "etag": '"sha-deadbeef"',
                }
            )
            assert r_store.get("stored") is True
            # handler reports whether the row is validatable
            assert r_store.get("validated") is True

            r_check = d.client.send({"cmd": "cached_mcp_check", "key": key})
            assert r_check.get("cached") is True
            assert r_check.get("data") == "origin-body"
            # the whole point: a gateway can now build If-None-Match
            assert r_check.get("etag") == '"sha-deadbeef"'

    def test_ipc_store_without_etag_reports_unvalidated(self):
        with E2EDaemon() as d:
            key = "phase-c-key-2"
            r_store = d.client.send({"cmd": "cached_mcp_store", "key": key, "content": "body"})
            assert r_store.get("stored") is True
            # loud signal: this row can never be revalidated
            assert r_store.get("validated") is False

            r_check = d.client.send({"cmd": "cached_mcp_check", "key": key})
            assert r_check.get("cached") is True
            assert r_check.get("etag") is None


class TestValidateBeforeServeIsImplementable:
    """The policy doc's loop must be executable purely over IPC."""

    def test_full_s3_loop_over_ipc(self):
        with E2EDaemon() as d:
            key = "phase-c-s3"

            # 1. miss -> origin call -> store with validator
            assert d.client.send({"cmd": "cached_mcp_check", "key": key}).get("cached") is False
            d.client.send(
                {
                    "cmd": "cached_mcp_store",
                    "key": key,
                    "content": "v1",
                    "etag": '"e1"',
                }
            )

            # 2. hit -> caller gets etag -> conditional request -> 304
            hit = d.client.send({"cmd": "cached_mcp_check", "key": key})
            assert hit["etag"] == '"e1"'
            # (origin would answer 304 here; serve hit["data"])
            assert hit["data"] == "v1"

            # 3. origin changed -> 200 + new body -> overwrite row
            d.client.send(
                {
                    "cmd": "cached_mcp_store",
                    "key": key,
                    "content": "v2",
                    "etag": '"e2"',
                }
            )
            after = d.client.send({"cmd": "cached_mcp_check", "key": key})
            assert after["data"] == "v2"
            assert after["etag"] == '"e2"'


def test_hermetic_subprocess_default_still_sha256():
    """The Phase B/C substrate assumption: daemon keys are sha256."""
    code = (
        "import toolrecall._db as db;"
        "import hashlib;"
        "assert db._hash('x') == hashlib.sha256(b'x').hexdigest();"
        "print('ok')"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert "ok" in out.stdout
