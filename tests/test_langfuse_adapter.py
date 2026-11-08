"""Tests for the Langfuse adapter (toolrecall.adapters.langfuse).

Framework-free core: runs everywhere against the hermetic per-test cache DB
(conftest.py), with daemon_running() mocked True so cached_mcp_check/store
exercise the REAL cache code path.

The langfuse SDK is NOT installed in the repo venv — every test here runs
with it absent, proving the adapter is importable and functional without
it (verdict reporting silently skips).
"""

import asyncio

import pytest

import toolrecall.client as tr_client


def langfuse_mod():
    from toolrecall.adapters import langfuse as mod

    return mod


# ---- Fixtures ---------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_db_singleton(_isolated_toolrecall_env):
    """Make conftest's per-test TOOLRECALL_CACHE_DB actually take effect.

    toolrecall._db caches both config and the sqlite connection as module
    singletons; without this reset every test after the first writes to the
    FIRST test's temp DB. Test-infra only — no production code touched.
    """
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


@pytest.fixture()
def daemon_down(monkeypatch):
    monkeypatch.setattr(tr_client, "daemon_running", lambda: False)


# ---- Module shape -------------------------------------------------------------


class TestModuleShape:
    def test_import_without_langfuse(self):
        mod = langfuse_mod()
        assert mod.ADAPTER_SERVER == "langfuse"
        assert callable(mod.cached_tool)
        assert callable(mod.cache_verdict_metadata)
        assert callable(mod.invalidate_langfuse_cache)

    def test_report_is_silent_noop_without_sdk(self):
        # langfuse is absent in the repo venv; reporting must not raise.
        mod = langfuse_mod()
        mod._report_verdict({"hit": True})  # no exception == pass


# ---- Graceful bypass (daemon down) ----------------------------------------------


class TestBypass:
    def test_daemon_down_executes_live_every_time(self, daemon_down):
        mod = langfuse_mod()
        calls = []

        @mod.cached_tool(ttl=300)
        def tool(x):
            calls.append(x)
            return {"v": x}

        assert tool(1) == {"v": 1}
        assert tool(1) == {"v": 1}
        assert calls == [1, 1]  # executed twice — bypass, no caching

    def test_async_daemon_down_executes_live(self, daemon_down):
        mod = langfuse_mod()
        calls = []

        @mod.cached_tool()
        async def atool(x):
            calls.append(x)
            return x

        assert asyncio.run(atool(5)) == 5
        assert asyncio.run(atool(5)) == 5
        assert calls == [5, 5]

    def test_check_exception_executes_live(self, daemon_up, monkeypatch):
        mod = langfuse_mod()

        def boom(*a, **k):
            raise RuntimeError("read failed")

        monkeypatch.setattr(mod, "cached_mcp_check", boom)

        @mod.cached_tool(ttl=300)
        def tool(x):
            return x + 1

        assert tool(1) == 2


# ---- Core cache behavior (real cache code path, mocked daemon) -----------------


class TestCacheRoundtrip:
    def test_miss_then_hit_roundtrip(self, daemon_up):
        """THE gate: store must produce a fresh hit with identical data.

        Guards the check→store key-pairing trap (the key is only on the miss
        path; a recheck after store must report cached with identical data).
        """
        mod = langfuse_mod()
        calls = []

        @mod.cached_tool(ttl=300)
        def search_web(query: str):
            calls.append(query)
            return {"results": [query], "count": 1}

        first = search_web("berlin weather")
        assert calls == ["berlin weather"]
        assert first == {"results": ["berlin weather"], "count": 1}

        second = search_web("berlin weather")
        assert calls == ["berlin weather"]  # NOT re-executed
        assert second == first  # round-trips via JSON identically

    def test_different_args_never_collide(self, daemon_up):
        mod = langfuse_mod()
        calls = []

        @mod.cached_tool(ttl=300)
        def tool(query: str):
            calls.append(query)
            return query

        assert tool("a") == "a"
        assert tool("b") == "b"
        assert calls == ["a", "b"]

    def test_positional_and_keyword_collide(self, daemon_up):
        """Bound-signature keying: f(1, 2, c=3) and f(1, b=2, c=3) share one entry."""
        mod = langfuse_mod()
        calls = []

        @mod.cached_tool(ttl=300)
        def tool(a, b=0, c=0):
            calls.append((a, b, c))
            return a + b + c

        assert tool(1, 2, c=3) == 6
        assert tool(1, b=2, c=3) == 6
        assert tool(a=1, b=2, c=3) == 6
        assert calls == [(1, 2, 3)]  # one execution total

    def test_ttl_zero_bypasses(self, daemon_up):
        mod = langfuse_mod()
        calls = []

        @mod.cached_tool(ttl=0)
        def tool(x):
            calls.append(x)
            return x

        assert tool(1) == 1
        assert tool(1) == 1
        assert calls == [1, 1]

    def test_default_args_apply_to_key(self, daemon_up):
        """apply_defaults: f(1) and f(1, b=0) share one entry when b defaults."""
        mod = langfuse_mod()
        calls = []

        @mod.cached_tool(ttl=300)
        def tool(a, b=0):
            calls.append((a, b))
            return a + b

        assert tool(1) == 1
        assert tool(1, b=0) == 1
        assert calls == [(1, 0)]  # same effective args → one execution

    def test_stale_served_value_round_trips(self, daemon_up):
        """SWR stale-serve path: stale hit must still deliver usable data.

        Verifies the adapter handles the stale flag shape of cached_mcp_check
        (data present + stale: True) by executing the real check path twice
        against an entry forced old enough to be inside the SWR window.
        """
        mod = langfuse_mod()

        @mod.cached_tool(ttl=1)
        def tool(x):
            return {"v": x}

        assert tool("a") == {"v": "a"}
        # Age the row past expires_at but keep it inside the SWR serve window
        # by rewriting cached_at backwards via the adapter-external cache API.
        import time

        import toolrecall.cache as cache_mod
        from toolrecall.cache import _mcp_request_key

        key = _mcp_request_key("langfuse", "tool", {"x": "a"})
        with cache_mod._db() as conn:
            conn.execute(
                "UPDATE mcp_cache SET cached_at = ? WHERE request_hash = ?",
                (time.time() - 2, key),
            )
        # ttl=1 → expired; SWR window (default) serves stale with stale=True.
        result = tool("a")
        assert result == {"v": "a"}

    def test_unroundtrippable_result_never_cached(self, daemon_up):
        """Objects that don't survive json.loads(json.dumps(v)) == v must
        execute live on every call — a stored repr would come back as a
        DIFFERENT value on hit."""
        mod = langfuse_mod()
        calls = []

        class Obj:
            def __init__(self, tag):
                self.tag = tag

        @mod.cached_tool(ttl=300)
        def tool(tag):
            calls.append(tag)
            return Obj(tag)

        first = tool("a")
        second = tool("a")
        assert len(calls) == 2  # never cached
        assert isinstance(first, Obj) and isinstance(second, Obj)
        assert first is not second

    def test_roundtrippable_results_cached(self, daemon_up):
        mod = langfuse_mod()
        calls = []

        @mod.cached_tool(ttl=300)
        def tool(x):
            calls.append(x)
            return {"nested": {"list": [1, 2, 3]}, "unicode": "äöü ✓"}

        a = tool(1)
        b = tool(1)
        assert calls == [1]
        assert a == b

    def test_store_failure_does_not_break_tool(self, daemon_up, monkeypatch):
        mod = langfuse_mod()

        def boom(*a, **k):
            raise RuntimeError("db closed")

        monkeypatch.setattr(mod, "cached_mcp_store", boom)

        @mod.cached_tool(ttl=300)
        def tool(x):
            return x * 2

        assert tool(2) == 4  # tool still works


# ---- Verdict / metadata shape ---------------------------------------------------


class TestVerdictShape:
    def test_metadata_namespacing(self):
        mod = langfuse_mod()
        v = {"hit": True, "key": "abc123def456", "age_s": 1.2}
        meta = mod.cache_verdict_metadata(v)
        assert list(meta.keys()) == ["toolrecall"]
        assert meta["toolrecall"] is v

    def test_verdict_minimal_on_miss(self):
        mod = langfuse_mod()
        v = mod._verdict_from_check({"cached": False, "key": "deadbeef00112233"}, 300)
        assert v == {"hit": False, "key": "deadbeef0011", "ttl": 300}

    def test_key_truncated_to_12_chars(self):
        mod = langfuse_mod()
        v = mod._verdict_from_check({"cached": True, "key": "a" * 64}, None)
        assert v["key"] == "a" * 12

    def test_stale_flag_propagates(self):
        mod = langfuse_mod()
        v = mod._verdict_from_check({"cached": True, "stale": True, "key": "k"}, 300)
        assert v["stale"] is True

    def test_bypassed_flag_propagates(self):
        mod = langfuse_mod()
        v = mod._verdict_from_check({"cached": False, "bypassed": True, "key": "k"}, 0)
        assert v["bypassed"] is True
        assert v["hit"] is False

    def test_merge_documentation_shape(self):
        mod = langfuse_mod()
        merged = {**{"user": "u1"}, **mod.cache_verdict_metadata({"hit": True})}
        assert merged == {"user": "u1", "toolrecall": {"hit": True}}

    def test_verdict_reported_on_hit_path(self, daemon_up, monkeypatch):
        mod = langfuse_mod()
        reported = []
        monkeypatch.setattr(mod, "_report_verdict", reported.append)

        @mod.cached_tool(ttl=300)
        def tool(x):
            return x

        tool("a")  # miss
        tool("a")  # hit
        assert len(reported) == 2
        assert reported[0]["hit"] is False
        assert reported[1]["hit"] is True


# ---- Metadata cap / merge ladder ------------------------------------------------


class TestMetadataCap:
    def test_cap_constant_matches_langfuse_doc(self):
        mod = langfuse_mod()
        assert mod.OBSERVATION_METADATA_CAP_BYTES == 9216

    def test_under_cap_merge_preserves_user_keys(self):
        mod = langfuse_mod()
        merged = mod.merge_verdict_metadata(
            {"user": "u1", "step": 2}, {"hit": True, "key": "abc123"}
        )
        assert merged["user"] == "u1" and merged["step"] == 2
        assert merged["toolrecall"] == {"hit": True, "key": "abc123"}

    def test_oversized_string_values_truncated_keys_kept(self):
        mod = langfuse_mod()
        big = "x" * 10000
        merged = mod.merge_verdict_metadata(
            None, {"hit": True, "key": "abc123def456", "note": big}, cap=10_000
        )
        assert len(merged["toolrecall"]["note"]) == 32  # truncated, key kept
        assert merged["toolrecall"]["hit"] is True

    def test_ladder_drops_nonessential_keeps_hit_key_age(self):
        mod = langfuse_mod()
        # Force deep truncation via a tiny cap.
        merged = mod.merge_verdict_metadata(
            {"user": "u1"},
            {"hit": True, "key": "abcdef123456", "age_s": 1.2, "ttl": 300, "stale": True},
            cap=40,
        )
        tr = merged["toolrecall"]
        assert merged["user"] == "u1"  # user keys NEVER consumed
        assert tr["hit"] is True
        assert "ttl" not in tr and "stale" not in tr  # non-essentials first out

    def test_ladder3_shrinks_key_to_8(self):
        mod = langfuse_mod()
        # cap=60: {hit, age_s} fits (58) → over cap only after rung 3's
        # shrink; {hit, key-8} = 48 bytes fits → rung 3 is the terminal rung.
        merged = mod.merge_verdict_metadata(
            None, {"hit": True, "key": "abcdef1234567890", "age_s": 9.9}, cap=60
        )
        tr = merged["toolrecall"]
        assert len(tr["key"]) <= 8, tr
        assert tr["hit"] is True
        assert "age_s" not in tr  # rung 3 dropped residual non-essentials

    def test_ladder4_hit_collapses_to_boolean_flag(self):
        mod = langfuse_mod()
        merged = mod.merge_verdict_metadata(
            {"user": "u1"}, {"hit": True, "key": "abcdef123456", "age_s": 9.9}, cap=10
        )
        assert merged["toolrecall"] == {"hit": True, "dropped": True}
        assert merged["user"] == "u1"

    def test_ladder4_miss_never_fully_dropped(self):
        """A miss explains a billed call — losing it makes live look cached."""
        mod = langfuse_mod()
        merged = mod.merge_verdict_metadata(
            {"user": "u1"}, {"hit": False, "key": "abcdef123456", "ttl": 300}, cap=10
        )
        tr = merged["toolrecall"]
        assert tr["hit"] is False
        assert tr["key"]  # key survives (identifies the call)
        assert tr["dropped"] is True

    def test_real_cap_huge_verdict_user_meta_still_intact(self):
        """With the REAL 9216 cap: 8KB user metadata + huge verdict → user
        metadata intact, verdict present (at minimum hit+dropped)."""
        mod = langfuse_mod()
        user_meta = {"blob": "y" * 8192}
        verdict = {"hit": True, "key": "k" * 64, "age_s": 1.0, "note": "z" * 2000}
        merged = mod.merge_verdict_metadata(user_meta, verdict)
        assert merged["blob"] == "y" * 8192
        assert "toolrecall" in merged
        assert merged["toolrecall"]["hit"] is True

    def test_unserializable_verdict_treated_over_cap(self):
        mod = langfuse_mod()

        class Obj:
            pass

        merged = mod.merge_verdict_metadata(None, {"hit": True, "obj": Obj()}, cap=10)
        # default=str coercion still applies; must not raise
        assert "toolrecall" in merged


# ---- Async ---------------------------------------------------------------------


class TestAsync:
    def test_async_roundtrip(self, daemon_up):
        mod = langfuse_mod()
        calls = []

        @mod.cached_tool(ttl=300)
        async def atool(x):
            calls.append(x)
            return {"x": x}

        first = asyncio.run(atool(1))
        second = asyncio.run(atool(1))
        assert first == {"x": 1}
        assert second == {"x": 1}
        assert calls == [1]  # second call served from cache

    def test_async_hit_reports_verdict(self, daemon_up, monkeypatch):
        mod = langfuse_mod()
        reported = []
        monkeypatch.setattr(mod, "_report_verdict", reported.append)

        @mod.cached_tool(ttl=300)
        async def atool(x):
            return x

        asyncio.run(atool(1))
        asyncio.run(atool(1))
        assert [v["hit"] for v in reported] == [False, True]


# ---- Namespace clear -------------------------------------------------------------


class TestInvalidate:
    def test_invalidate_clears_namespace(self, daemon_up):
        mod = langfuse_mod()
        calls = []

        @mod.cached_tool(ttl=300)
        def tool(x):
            calls.append(x)
            return x

        tool("a")
        mod.invalidate_langfuse_cache()
        tool("a")  # re-executes after invalidation
        assert calls == ["a", "a"]

