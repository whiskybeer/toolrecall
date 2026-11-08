"""Shared-namespace, validator-gated MCP serving.

Policy surface derived from the 2026-09-20 Phase C cache-correctness bench
(mcp-replay-bench): on a mutating origin, clock-based serving delivered ~29%
wrong results including success<->error inversions, while validate-before-serve
delivered 0% at 5.7x origin execution reduction.

Design invariants pinned here:

1. ``local`` keys are UNCHANGED from previous releases (existing rows keep
   hitting) — the shared scope is an extra namespace, not a migration.
2. A shared row and a local row of the same server/tool/args never collide.
3. ``[mcp] require_validator`` turns un-validatable rows into a loud
   ``reason="unvalidated"`` miss instead of a clock-based serve.
4. ``cached_mcp_serve`` is read-only, shared-namespace only, and refuses
   validator-less rows loudly.
5. Over IPC: store remains local-only (absent from the remote allowlist);
   serve is remotely allowed and cannot reach private rows.
"""

import hashlib
import importlib

import pytest

from tests.e2e_helpers import E2EDaemon


@pytest.fixture
def cache_mod(tmp_path, monkeypatch):
    monkeypatch.setenv("TOOLRECALL_CACHE_DB", str(tmp_path / "c.db"))
    import toolrecall._db as db_mod
    import toolrecall.cache as c

    importlib.reload(db_mod)
    importlib.reload(c)
    return c


class TestScopeNamespaces:
    def test_local_scope_key_is_unchanged(self, cache_mod):
        """Golden: the default namespace must keep its historical hash."""
        key = cache_mod._mcp_request_key("srv", "read", {"p": "a.py"})
        expected = hashlib.sha256(b'srv://read?{"p": "a.py"}').hexdigest()
        assert key == expected, "local scope key derivation changed — breaks existing rows"

    def test_shared_and_local_never_collide(self, cache_mod):
        local = cache_mod._mcp_request_key("srv", "read", {"p": "a.py"})
        shared = cache_mod._mcp_request_key("srv", "read", {"p": "a.py"}, scope="shared")
        assert local != shared

    def test_invalid_scope_rejected(self, cache_mod):
        with pytest.raises(ValueError):
            cache_mod._mcp_request_key("srv", "read", {}, scope="bad scope!")

    def test_rows_are_namespace_isolated(self, cache_mod):
        args = {"p": "a.py"}
        miss = cache_mod.cached_mcp_check("srv", "read", args)
        cache_mod.cached_mcp_store(miss["key"], "srv", "read", args, "LOCAL", etag='"L"')
        # a shared-scope check must not see the local row
        shared = cache_mod.cached_mcp_check("srv", "read", args, scope="shared")
        assert shared["cached"] is False
        # and the local row is still there
        assert cache_mod.cached_mcp_check("srv", "read", args)["data"] == "LOCAL"


class TestRequireValidatorPolicy:
    def test_default_off_serves_unvalidated_rows(self, cache_mod):
        args = {"p": "u.py"}
        miss = cache_mod.cached_mcp_check("srv", "read", args)
        cache_mod.cached_mcp_store(miss["key"], "srv", "read", args, "body")  # no etag
        hit = cache_mod.cached_mcp_check("srv", "read", args)
        assert hit["cached"] is True, "default must stay permissive for local caches"
        assert hit["etag"] is None

    def test_enabled_refuses_unvalidated_loudly(self, cache_mod, monkeypatch):
        args = {"p": "v.py"}
        miss = cache_mod.cached_mcp_check("srv", "read", args)
        cache_mod.cached_mcp_store(miss["key"], "srv", "read", args, "body")  # no etag

        monkeypatch.setattr(
            cache_mod.config,
            "get",
            lambda *a, **k: True if a[:2] == ("mcp", "require_validator") else False,
        )
        hit = cache_mod.cached_mcp_check("srv", "read", args)
        assert hit["cached"] is False
        assert hit["reason"] == "unvalidated"

    def test_enabled_still_serves_validated_rows(self, cache_mod, monkeypatch):
        args = {"p": "w.py"}
        miss = cache_mod.cached_mcp_check("srv", "read", args)
        cache_mod.cached_mcp_store(miss["key"], "srv", "read", args, "body", etag='"E1"')
        monkeypatch.setattr(
            cache_mod.config,
            "get",
            lambda *a, **k: True if a[:2] == ("mcp", "require_validator") else False,
        )
        hit = cache_mod.cached_mcp_check("srv", "read", args)
        assert hit["cached"] is True
        assert hit["etag"] == '"E1"'


class TestSharedServe:
    def _store_shared(self, cache_mod, args, data, etag):
        miss = cache_mod.cached_mcp_check("srv", "read", args, scope="shared")
        cache_mod.cached_mcp_store(miss["key"], "srv", "read", args, data, etag=etag)

    def test_serves_validated_shared_row(self, cache_mod):
        args = {"p": "s.py"}
        self._store_shared(cache_mod, args, "shared-body", '"E9"')
        r = cache_mod.cached_mcp_serve("srv", "read", args)
        assert r["served"] is True
        assert r["data"] == "shared-body"
        assert r["etag"] == '"E9"'
        assert r["key"]

    def test_refuses_unvalidated_shared_row(self, cache_mod):
        args = {"p": "s2.py"}
        self._store_shared(cache_mod, args, "shared-body", None)
        r = cache_mod.cached_mcp_serve("srv", "read", args)
        assert r["served"] is False
        assert r["reason"] == "unvalidated"

    def test_miss_reported_loudly(self, cache_mod):
        r = cache_mod.cached_mcp_serve("srv", "read", {"p": "absent.py"})
        assert r["served"] is False
        assert r["reason"] == "miss"

    def test_serve_cannot_reach_local_rows(self, cache_mod):
        args = {"p": "private.py"}
        miss = cache_mod.cached_mcp_check("srv", "read", args)  # local scope
        cache_mod.cached_mcp_store(miss["key"], "srv", "read", args, "PRIVATE", etag='"P"')
        r = cache_mod.cached_mcp_serve("srv", "read", args)  # shared scope
        assert r["served"] is False, "a private row must never be visible to the shared tier"
        assert r["reason"] == "miss"

    def test_serve_does_not_write(self, cache_mod):
        """Read-only invariant: unknown rows are not created by serving."""
        args = {"p": "nowrite.py"}
        before = cache_mod.cached_mcp_serve("srv", "read", args)
        assert before["served"] is False
        after = cache_mod.cached_mcp_serve("srv", "read", args)
        assert after["served"] is False, "serve must never populate the cache"


class TestSharedServeOverIPC:
    def test_serve_round_trip(self):
        with E2EDaemon() as d:
            key = "shared-k1"
            d.client.send(
                {
                    "cmd": "cached_mcp_store",
                    "key": key,
                    "content": "shared-bytes",
                    "etag": '"E5"',
                    "scope": "shared",
                }
            )
            # key mode: rows written through the generic IPC facade
            r = d.client.send({"cmd": "cached_mcp_serve", "key": key})
            assert r["served"] is True
            assert r["data"] == "shared-bytes"
            assert r["etag"] == '"E5"'

    def test_store_is_not_remote_allowed(self):
        """The write primitive must stay local-only."""
        from toolrecall.daemon import REMOTE_ALLOWED_COMMANDS

        assert "cached_mcp_serve" in REMOTE_ALLOWED_COMMANDS
        assert "cached_mcp_store" not in REMOTE_ALLOWED_COMMANDS
        assert "cached_mcp_check" not in REMOTE_ALLOWED_COMMANDS
        assert "mcp_call" not in REMOTE_ALLOWED_COMMANDS
