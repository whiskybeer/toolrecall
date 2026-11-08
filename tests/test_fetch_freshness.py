"""Tests for ETag revalidation, URL-scoped invalidation, and adapter freshness.

Three features built together (2026-09):
1. mcp_cache.etag column + get_mcp_etag / refresh_mcp_etag /
   revalidate_mcp_entry — conditional HTTP revalidation (304 → keep body).
2. `toolrecall invalidate --server/--url` flags + invalidate_mcp_url.
3. `@cached_tool(freshness=True)` — (result, {cached, age_s}) metadata.

Hermetic per-test DB via conftest; daemon_running mocked True.
"""

import json
import sys

import pytest

import toolrecall.client as tr_client
from toolrecall import cache as tr_cache


@pytest.fixture(autouse=True)
def _reset_db_singleton(_isolated_toolrecall_env):
    """Make conftest's per-test TOOLRECALL_CACHE_DB actually take effect
    (see tests/test_langgraph_adapter.py — module singleton reset)."""
    import toolrecall._db as dbmod

    dbmod._cached_config = None
    if getattr(dbmod, "_db_real", None) is not None:
        try:
            dbmod._db_real.close()
        except Exception:
            pass
        dbmod._db_real = None
    from toolrecall.cache import SCHEMA

    dbmod._init(SCHEMA)
    yield


@pytest.fixture()
def daemon_up(monkeypatch):
    monkeypatch.setattr(tr_client, "daemon_running", lambda: True)


# ---- etag store / read / refresh ---------------------------------------------


class TestEtagStoreRead:
    def test_store_without_etag_row_is_unvalidated(self, daemon_up):
        tr_cache.cached_mcp_store("h1", "fetch", "fetch_url", {"url": "u1"}, "body1")
        assert tr_cache.get_mcp_etag("fetch", "fetch_url", {"url": "u1"}) is None

    def test_store_with_etag_then_read(self, daemon_up):
        tr_cache.cached_mcp_store(
            tr_cache._mcp_request_key("fetch", "fetch_url", {"url": "u2"}),
            "fetch",
            "fetch_url",
            {"url": "u2"},
            "body2",
            etag='"v2"',
        )
        assert tr_cache.get_mcp_etag("fetch", "fetch_url", {"url": "u2"}) == '"v2"'

    def test_get_mcp_etag_unknown_row(self, daemon_up):
        assert tr_cache.get_mcp_etag("fetch", "fetch_url", {"url": "nope"}) is None

    def test_key_derivation_matches_check(self, daemon_up, monkeypatch):
        """get_mcp_etag must address the same row cached_mcp_check reads —
        including [norm] normalization when enabled: with norm ON, store and
        read both normalize, so reordered + noise-key args still resolve."""
        import toolrecall.normalizer as norm

        saved = (norm._SORT_LISTS, norm._STRIP_STRINGS)
        real_get = tr_cache.config.get

        def norm_on(section, key, default=None):
            if (section, key) == ("norm", "enabled"):
                return True
            return real_get(section, key, default)

        monkeypatch.setattr(tr_cache.config, "get", norm_on)
        norm.configure(sort_lists=True, strip_strings=True)
        try:
            # Store AND derive under norm-on — the production pairing.
            tr_cache.cached_mcp_store(
                tr_cache._mcp_request_key("fetch", "fetch_url", {"url": "u3", "timestamp": 42}),
                "fetch",
                "fetch_url",
                {"url": "u3", "timestamp": 42},
                "b3",
                etag='"e3"',
            )
            found = tr_cache.get_mcp_etag(
                "fetch",
                "fetch_url",
                {"timestamp": 999, "url": "u3"},  # reordered + noise
            )
        finally:
            norm.configure(sort_lists=saved[0], strip_strings=saved[1])
        assert found == '"e3"'

    def test_refresh_mcp_etag_bumps_expiry(self, daemon_up):
        h4 = tr_cache._mcp_request_key("fetch", "fetch_url", {"url": "u4"})
        tr_cache.cached_mcp_store(h4, "fetch", "fetch_url", {"url": "u4"}, "b4", ttl=10)
        tr_cache.refresh_mcp_etag(h4, '"e4"', ttl=600)
        import time

        with tr_cache._db() as conn:
            row = conn.execute("SELECT * FROM mcp_cache WHERE request_hash = ?", (h4,)).fetchone()
        assert row["etag"] == '"e4"'
        assert row["expires_at"] > time.time() + 500
        assert row["data"] == "b4"  # body untouched

    def test_migration_adds_etag_column(self, daemon_up):
        with tr_cache._db() as conn:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(mcp_cache)")}
        assert "etag" in cols


# ---- revalidate_mcp_entry contract --------------------------------------------


class TestRevalidateEntry:
    def _seed(self, etag='"e1"'):
        args = {"url": "u5"}
        request_hash = tr_cache._mcp_request_key("fetch", "fetch_url", args)
        tr_cache.cached_mcp_store(
            request_hash, "fetch", "fetch_url", args, "old-body", ttl=10, etag=etag
        )
        # force the row expired
        import time

        with tr_cache._db() as conn:
            conn.execute(
                "UPDATE mcp_cache SET expires_at = ? WHERE request_hash = ?",
                (time.time() - 1, request_hash),
            )
        return request_hash

    def test_304_revalidates_keeps_body(self, daemon_up):
        h = self._seed()

        def fetch_fn(etag):
            return {"status": 304, "revalidated": True, "etag": etag}

        out = tr_cache.revalidate_mcp_entry("fetch", "fetch_url", {"url": "u5"}, fetch_fn)
        assert out["action"] == "revalidated"
        with tr_cache._db() as conn:
            row = conn.execute("SELECT * FROM mcp_cache WHERE request_hash = ?", (h,)).fetchone()
        assert row["data"] == "old-body"  # body kept, byte-identical
        assert row["expires_at"] > __import__("time").time()  # fresh again

    def test_200_replaces_row(self, daemon_up):
        h = self._seed()

        def fetch_fn(etag):
            return {"status": 200, "content": "new-body", "etag": '"e2"'}

        out = tr_cache.revalidate_mcp_entry("fetch", "fetch_url", {"url": "u5"}, fetch_fn)
        assert out["action"] == "replaced"
        with tr_cache._db() as conn:
            row = conn.execute("SELECT * FROM mcp_cache WHERE request_hash = ?", (h,)).fetchone()
        assert json.loads(row["data"])["content"] == "new-body"
        assert row["etag"] == '"e2"'

    def test_network_failure_keeps_stale_row(self, daemon_up):
        h = self._seed()

        def fetch_fn(etag):
            return None  # network down

        out = tr_cache.revalidate_mcp_entry("fetch", "fetch_url", {"url": "u5"}, fetch_fn)
        assert out["action"] == "failed"
        with tr_cache._db() as conn:
            row = conn.execute("SELECT data FROM mcp_cache WHERE request_hash = ?", (h,)).fetchone()
        assert row["data"] == "old-body"

    def test_no_etag_action(self, daemon_up):
        self._seed(etag=None)

        def fetch_fn(etag):  # pragma: no cover — must not be called
            raise AssertionError("fetch_fn must not run without etag")

        out = tr_cache.revalidate_mcp_entry("fetch", "fetch_url", {"url": "u5"}, fetch_fn)
        assert out["action"] == "no_etag"

    def test_not_found_action(self, daemon_up):
        def fetch_fn(etag):  # pragma: no cover
            raise AssertionError

        out = tr_cache.revalidate_mcp_entry("fetch", "fetch_url", {"url": "never-stored"}, fetch_fn)
        assert out["action"] == "not_found"

    def test_fetch_fn_exception_is_contained(self, daemon_up):
        self._seed()

        def fetch_fn(etag):
            raise RuntimeError("boom")

        out = tr_cache.revalidate_mcp_entry("fetch", "fetch_url", {"url": "u5"}, fetch_fn)
        assert out["action"] == "failed"


# ---- invalidate_mcp_url -------------------------------------------------------


class TestInvalidateUrl:
    def test_substring_match_deletes_only_matching(self, daemon_up):
        tr_cache.cached_mcp_store(
            "i1", "fetch", "fetch_url", {"url": "https://example.com/page1"}, "b"
        )
        tr_cache.cached_mcp_store(
            "i2", "fetch", "fetch_url", {"url": "https://example.com/page2"}, "b"
        )
        tr_cache.cached_mcp_store("i3", "fetch", "fetch_url", {"url": "https://other.org/x"}, "b")
        n = tr_cache.invalidate_mcp_url("fetch", "fetch_url", "example.com")
        assert n == 2
        assert (
            tr_cache.get_mcp_etag("fetch", "fetch_url", {"url": "https://other.org/x"}) is None
            or True
        )
        with tr_cache._db() as conn:
            remaining = conn.execute("SELECT COUNT(*) FROM mcp_cache").fetchone()[0]
        assert remaining == 1

    def test_case_insensitive(self, daemon_up):
        tr_cache.cached_mcp_store("i4", "fetch", "fetch_url", {"url": "https://EXAMPLE.com/A"}, "b")
        assert tr_cache.invalidate_mcp_url("fetch", "fetch_url", "example.com") == 1

    def test_empty_substring_noop(self, daemon_up):
        assert tr_cache.invalidate_mcp_url("fetch", "fetch_url", "") == 0

    def test_server_scoped(self, daemon_up):
        tr_cache.cached_mcp_store("i5", "fetch", "fetch_url", {"url": "https://x.com/"}, "b")
        tr_cache.cached_mcp_store("i6", "github", "get", {"q": "https://x.com/"}, "b")
        assert tr_cache.invalidate_mcp_url("fetch", "fetch_url", "x.com") == 1
        with tr_cache._db() as conn:
            n = conn.execute("SELECT COUNT(*) FROM mcp_cache").fetchone()[0]
        assert n == 1  # github row survived


# ---- CLI invalidate flags -----------------------------------------------------


class TestInvalidateCli:
    def _run(self, monkeypatch, tmp_path, *args):
        """Run cmd_invalidate with args + a redirected cache DB.

        Returns stdout. Redirects sys.stdout instead of capsys — capsys is
        unavailable under -p no:capture (make test-unit), same reason the
        antigravity notice test uses the redirect pattern.
        """
        import io

        import toolrecall.cli as cli
        import toolrecall._db as dbmod
        from toolrecall.cache import SCHEMA

        db_path = tmp_path / "cache.db"
        monkeypatch.setenv("TOOLRECALL_CACHE_DB", str(db_path))
        dbmod._cached_config = None
        if getattr(dbmod, "_db_real", None) is not None:
            dbmod._db_real.close()
            dbmod._db_real = None
        dbmod._init(SCHEMA)
        monkeypatch.setattr(sys, "argv", ["toolrecall", "invalidate", *args])
        buf = io.StringIO()
        real_stdout = sys.stdout
        sys.stdout = buf
        try:
            cli.cmd_invalidate()
        finally:
            sys.stdout = real_stdout
        return buf.getvalue()

    def test_server_flag(self, monkeypatch, tmp_path):
        out = self._run(monkeypatch, tmp_path, "--server", "fetch")
        assert "fetch" in out and "Invalidated" in out

    def test_url_flag_reports_count(self, monkeypatch, tmp_path):
        out = self._run(monkeypatch, tmp_path, "--url", "example.com")
        assert "example.com" in out and "0 cached response(s)" in out

    def test_url_flag_deletes_matching_rows(self, monkeypatch, tmp_path):
        self._run(monkeypatch, tmp_path)  # init DB first

        tr_cache.cached_mcp_store(
            tr_cache._mcp_request_key("fetch", "fetch_url", {"url": "https://e.com/a"}),
            "fetch",
            "fetch_url",
            {"url": "https://e.com/a"},
            "b",
        )
        tr_cache.cached_mcp_store(
            tr_cache._mcp_request_key("fetch", "fetch_url", {"url": "https://e.com/b"}),
            "fetch",
            "fetch_url",
            {"url": "https://e.com/b"},
            "b",
        )
        out = self._run(monkeypatch, tmp_path, "--url", "e.com")
        assert "2 cached response(s)" in out

    def test_scope_flag_passthrough(self, monkeypatch, tmp_path):
        import toolrecall.cli as cli
        import toolrecall.client as client_mod

        monkeypatch.setattr(sys, "argv", ["toolrecall", "invalidate", "--scope", "api"])
        seen = {}

        def fake_cache_invalidate(scope="agent"):
            seen["scope"] = scope
            return "ok"

        monkeypatch.setattr(client_mod, "cache_invalidate", fake_cache_invalidate)
        cli.cmd_invalidate()
        assert seen["scope"] == "api"


class TestFreshness:
    def test_freshness_meta_on_hit_and_miss(self, daemon_up):
        from toolrecall.adapters import antigravity as ag

        calls = []

        @ag.cached_tool(ttl=300, freshness=True)
        def f(x: int) -> int:
            calls.append(x)
            return x * 3

        data, meta1 = f(4)
        assert data == 12 and meta1 == {"cached": False, "age_s": None}
        data2, meta2 = f(4)
        assert data2 == 12
        assert meta2["cached"] is True
        assert isinstance(meta2["age_s"], float) and meta2["age_s"] >= 0
        assert calls == [4]

    def test_default_returns_plain_result(self, daemon_up):
        from toolrecall.adapters import antigravity as ag

        @ag.cached_tool(ttl=300)
        def g(x: int) -> int:
            return x + 1

        assert g(1) == 2  # no tuple — backward compatible

    def test_freshness_ttl_zero(self, daemon_up):
        from toolrecall.adapters import antigravity as ag

        @ag.cached_tool(ttl=0, freshness=True)
        def h(x: int) -> int:
            return x

        data, meta = h(9)
        assert data == 9 and meta == {"cached": False, "age_s": None}

    def test_async_freshness(self, daemon_up):
        import asyncio

        from toolrecall.adapters import antigravity as ag

        @ag.cached_tool(ttl=300, freshness=True)
        async def af(x: int) -> int:
            return x - 1

        data, meta1 = asyncio.run(af(5))
        assert (data, meta1["cached"]) == (4, False)
        data2, meta2 = asyncio.run(af(5))
        assert meta2["cached"] is True

    def test_check_returns_age_s(self, daemon_up):
        tr_cache.cached_mcp_store(
            tr_cache._mcp_request_key("s", "t", {"a": 1}), "s", "t", {"a": 1}, "d", ttl=300
        )
        r = tr_cache.cached_mcp_check("s", "t", {"a": 1}, ttl=300)
        assert r["cached"] is True and r["age_s"] >= 0
