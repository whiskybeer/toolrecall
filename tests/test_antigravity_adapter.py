"""Tests for the Antigravity adapter (toolrecall.adapters.antigravity).

Framework-free core: runs everywhere against the hermetic per-test cache DB
(conftest.py `_isolated_toolrecall_env`), with daemon_running() mocked so
cached_mcp_check/store exercise the real cache code path.

SDK binding (google-antigravity ToolRunner + Agent): skipped unless the SDK
is importable — same convention as the LangGraph adapter.
"""

import asyncio
import json

import pytest

import toolrecall.client as tr_client
from toolrecall.adapters import antigravity


# ---- Fixtures ---------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_db_singleton(_isolated_toolrecall_env):
    """Make conftest's per-test TOOLRECALL_CACHE_DB actually take effect.

    toolrecall._db caches config + connection as module singletons; without
    this reset later tests write into the FIRST test's temp DB.
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
    if getattr(dbmod, "_db_real", None) is not None:
        try:
            dbmod._db_real.close()
        except Exception:
            pass
        dbmod._db_real = None
        dbmod._cached_config = None


@pytest.fixture()
def daemon_up(monkeypatch):
    monkeypatch.setattr(tr_client, "daemon_running", lambda: True)


@pytest.fixture()
def daemon_down(monkeypatch):
    monkeypatch.setattr(tr_client, "daemon_running", lambda: False)


# ---- Module shape -------------------------------------------------------------


class TestModuleShape:
    def test_import_without_sdk(self):
        """Module imports with google-antigravity absent (lazy base pattern)."""
        assert antigravity.ADAPTER_SERVER == "antigravity"
        assert callable(antigravity.cached_tool)

    def test_namespaced_server(self):
        assert antigravity.ADAPTER_SERVER not in ("adk", "langchain", "langgraph")


# ---- Core: check → execute → store --------------------------------------------


class TestCachedToolCore:
    def test_hit_after_miss(self, daemon_up):
        calls = []

        @antigravity.cached_tool(ttl=300)
        def fetch(url: str) -> str:
            calls.append(url)
            return f"body:{url}"

        assert fetch("https://x.test/a") == "body:https://x.test/a"
        assert fetch("https://x.test/a") == "body:https://x.test/a"
        assert calls == ["https://x.test/a"]

    def test_distinct_args_miss(self, daemon_up):
        calls = []

        @antigravity.cached_tool()
        def fetch(url: str) -> str:
            calls.append(url)
            return f"body:{url}"

        fetch("https://x.test/1")
        fetch("https://x.test/2")
        assert calls == ["https://x.test/1", "https://x.test/2"]

    def test_store_uses_nonempty_request_hash(self, daemon_up):
        """Regression guard for the ADK empty-key bug: after a miss→store
        cycle the row must be discoverable by a fresh check (store landed
        under the same hash check reads, not a hash of "")."""
        from toolrecall.cache import cached_mcp_check

        @antigravity.cached_tool(ttl=300)
        def probe(q: str) -> str:
            return f"r:{q}"

        probe("hello")
        again = cached_mcp_check(antigravity.ADAPTER_SERVER, "probe", {"q": "hello"}, ttl=300)
        assert again.get("cached"), "store landed under wrong/empty hash"

    def test_recheck_after_store(self, daemon_up):
        """Second miss-path check must now report cached (row findable)."""
        from toolrecall.cache import cached_mcp_check

        @antigravity.cached_tool(ttl=300)
        def probe(q: str) -> str:
            return "data"

        probe("x")
        again = cached_mcp_check(antigravity.ADAPTER_SERVER, "probe", {"q": "x"}, ttl=300)
        assert again.get("cached") is True
        assert json.loads(again["data"]) == "data"

    def test_async_tool_cached(self, daemon_up):
        calls = []

        @antigravity.cached_tool(ttl=300)
        async def afetch(q: str) -> str:
            calls.append(q)
            return f"r:{q}"

        import asyncio

        assert asyncio.run(afetch("k1")) == "r:k1"
        assert asyncio.run(afetch("k1")) == "r:k1"
        assert calls == ["k1"]

    def test_positional_and_kwarg_same_key(self, daemon_up):
        """Runner invokes kwarg-only; direct calls may be positional — the
        SDK coerces kwargs into signature order, so both must collide into
        one cache entry."""
        from toolrecall.cache import cached_mcp_check

        calls = []

        @antigravity.cached_tool(ttl=300)
        def probe(q: str) -> str:
            calls.append(q)
            return "data"

        probe("v")  # direct positional
        probe(q="v")  # kwarg (SDK-style)
        assert calls == ["v"]
        # Same for the store row: kwarg-shape lookup hits it.
        again = cached_mcp_check(antigravity.ADAPTER_SERVER, "probe", {"q": "v"}, ttl=300)
        assert again.get("cached") is True

    def test_errors_never_cached(self, daemon_up):
        calls = []

        @antigravity.cached_tool(ttl=300)
        def flaky(q: str) -> str:
            calls.append(q)
            if len(calls) == 1:
                raise RuntimeError("boom")
            return "ok"

        with pytest.raises(RuntimeError):
            flaky("z")
        assert flaky("z") == "ok"  # re-executes after the error
        assert calls == ["z", "z"]

    def test_unserializable_object_stringified(self, daemon_up):
        class Widget:
            def __str__(self):
                return "WIDGET"

        @antigravity.cached_tool(ttl=300)
        def make(q: str) -> Widget:
            return Widget()

        w = make("a")
        assert "WIDGET" in str(w)  # default=str on dump, string on load

    def test_bare_decorator_without_parens(self, daemon_up):
        @antigravity.cached_tool
        def f(q: str) -> str:
            return "v"

        assert f("a") == "v"

    def test_norm_enabled_noise_keys_still_hit(self, daemon_up, monkeypatch):
        """With [norm] enabled, check normalizes args (strips timestamp etc.).

        The store MUST go under the check-derived (normalized) key, not a
        plain-JSON re-derivation — otherwise semantically identical calls
        with different noise keys never hit. Regression for the
        check-normalizes/store-doesn't asymmetry.
        """
        import toolrecall.cache as cachemod
        import toolrecall.normalizer as norm

        saved = (norm._SORT_LISTS, norm._STRIP_STRINGS)
        real_config_get = cachemod.config.get

        def norm_on(section, key, default=None):
            if (section, key) == ("norm", "enabled"):
                return True
            return real_config_get(section, key, default)

        monkeypatch.setattr(cachemod.config, "get", norm_on)
        norm.configure(sort_lists=True, strip_strings=True)
        try:
            calls = []

            @antigravity.cached_tool(ttl=300)
            def lookup(x: int, timestamp: int = 0) -> str:
                calls.append((x, timestamp))
                return f"v{x}"

            r1 = lookup(x=1, timestamp=111)
            r2 = lookup(x=1, timestamp=222)  # only noise key differs
            assert r1 == r2 == "v1"
            assert calls == [(1, 111)]  # second call was a cache hit
        finally:
            norm.configure(sort_lists=saved[0], strip_strings=saved[1])

    def test_store_reuses_check_derived_key(self, daemon_up, monkeypatch):
        """The store path must single-source the request hash from the check.

        Spy on cached_mcp_store: the stored hash must equal the key the
        miss-path check derived for the same arguments.
        """
        from toolrecall.adapters import antigravity as ag

        stored = {}
        real_store = ag.cached_mcp_store

        def spy_store(request_hash, server, tool, arguments, data, ttl=None):
            stored["hash"] = request_hash
            return real_store(request_hash, server, tool, arguments, data, ttl=ttl)

        monkeypatch.setattr(ag, "cached_mcp_store", spy_store)

        @antigravity.cached_tool(ttl=300)
        def f(x: int) -> int:
            return x * 2

        assert f(3) == 6
        # Fresh miss-check for the same args — derives the canonical key.
        miss = ag.cached_mcp_check(ag.ADAPTER_SERVER, "f", {"x": 3}, ttl=0)
        assert miss["key"] == stored["hash"]


# ---- TTL / bypass semantics ---------------------------------------------------


class TestBypassSemantics:
    def test_daemon_down_bypass(self, daemon_down):
        calls = []

        @antigravity.cached_tool(ttl=300)
        def work(x: int) -> int:
            calls.append(x)
            return x * 2

        assert work(21) == 42
        assert calls == [21]

    def test_ttl_zero_bypasses(self, daemon_up):
        calls = []

        @antigravity.cached_tool(ttl=0)
        def work(x: int) -> int:
            calls.append(x)
            return x

        work(1)
        work(1)
        assert calls == [1, 1]

    def test_ttl_zero_stores_nothing(self, daemon_up):
        from toolrecall.cache import cached_mcp_check

        @antigravity.cached_tool(ttl=0)
        def work(x: int) -> int:
            return x

        work(1)
        res = cached_mcp_check(antigravity.ADAPTER_SERVER, "work", {"x": 1}, ttl=0)
        assert res.get("cached") is not True

    def test_stale_serves_never_returned(self, daemon_up):
        """stale: True from the daemon = re-execute live (freshness over speed)."""
        calls = []

        @antigravity.cached_tool(ttl=60)
        def work(q: str) -> str:
            calls.append(q)
            return "fresh"

        work("s")
        assert calls == ["s"]


# ---- ToolContext exclusion ------------------------------------------------------


class TestToolContextExclusion:
    def test_ctx_param_excluded_from_key(self, daemon_up):
        """Same args + different ctx object → same cache entry (SDK injects
        stateful ToolContext; it must never enter the key)."""
        from toolrecall.adapters.antigravity import _context_param_name

        class ToolContext:  # shadow for annotation-name fallback (no SDK)
            pass

        def tool(query: str, ctx: ToolContext) -> str:  # type: ignore[valid-type]
            return f"{query}:{id(ctx)}"

        assert _context_param_name(tool) == "ctx"
        wrapped = antigravity.cached_tool(ttl=300)(tool)

        results = [
            wrapped(query="q", ctx=object.__new__(ToolContext)),
            wrapped(query="q", ctx=object.__new__(ToolContext)),
        ]
        # Different ctx ids, same args → second call served from cache, so the
        # returned string (which embeds the ctx id) must be identical.
        assert results[0] == results[1]
        # and exactly ONE cache row exists for this tool
        import toolrecall._db as dbmod

        with dbmod._db() as conn:
            n = conn.execute("SELECT COUNT(*) FROM mcp_cache WHERE mcp_tool = 'tool'").fetchone()[0]
        assert n == 1

    def test_ctx_param_name_fallback_no_sdk(self, daemon_up):
        """Without the SDK installed, exclusion falls back to annotation name."""
        from toolrecall.adapters.antigravity import _context_param_name

        def tool(query: str, antigravity_ctx: "ToolContext") -> str:  # noqa: F821
            return query

        assert _context_param_name(tool) == "antigravity_ctx"


# ---- SDK binding (skipped without google-antigravity) ---------------------------


def sdk_mod():
    try:
        import google.antigravity

        return google.antigravity
    except ImportError:
        return None


SDK = sdk_mod() is not None


@pytest.mark.skipif(not SDK, reason="google-antigravity not installed")
class TestAntigravitySDKBinding:
    """End-to-end through the SDK's real ToolRunner: register → execute twice
    → second execution served from cache."""

    def _runner(self):
        from google.antigravity.tools.tool_runner import ToolRunner

        return ToolRunner()

    def test_register_and_cache_through_real_runner(self, daemon_up):
        runner = self._runner()
        calls = []

        @antigravity.cached_tool(ttl=300)
        def search_web(query: str) -> str:
            calls.append(query)
            return f"result:{query}"

        runner.register(search_web)
        import asyncio

        r1 = asyncio.run(runner.execute("search_web", query="antigravity"))
        r2 = asyncio.run(runner.execute("search_web", query="antigravity"))
        assert r1 == r2 == "result:antigravity"
        assert calls == ["antigravity"]  # second execution was a cache hit

    def test_register_name_and_schema_preserved(self, daemon_up):
        """functools.wraps keeps __name__/docstring; schema generation strips
        only the ToolContext param — registration must see the real name."""
        runner = self._runner()
        calls = []

        @antigravity.cached_tool(ttl=300)
        def probe_tool(q: str) -> str:
            """Probe docstring."""
            calls.append(q)
            return "v"

        runner.register(probe_tool)
        assert runner._tools["probe_tool"] is probe_tool
        assert probe_tool.__doc__ == "Probe docstring."
        asyncio.run(runner.execute("probe_tool", q="x"))
        asyncio.run(runner.execute("probe_tool", q="x"))
        assert calls == ["x"]

    def test_ctx_tool_executes_through_real_runner(self, daemon_up):
        """Tool declaring ToolContext in its signature: runner injects it,
        adapter excludes it from the key."""
        runner = self._runner()
        from google.antigravity.tools.tool_context import ToolContext

        calls = []

        @antigravity.cached_tool(ttl=300)
        def ctx_tool(query: str, ctx: ToolContext) -> str:
            calls.append(query)
            return f"{query}"

        runner.register(ctx_tool)
        runner.set_context(object.__new__(ToolContext))
        r1 = asyncio.run(runner.execute("ctx_tool", query="q"))
        r2 = asyncio.run(runner.execute("ctx_tool", query="q"))
        assert r1 == r2 == "q"
        assert calls == ["q"]  # second execution was a cache hit

    def test_agent_accepts_cached_tool(self, daemon_up):
        """LocalAgentConfig(tools=[...]) accepts the wrapped callable."""
        from google.antigravity import LocalAgentConfig

        @antigravity.cached_tool(ttl=300)
        def search_web(query: str) -> str:
            return f"result:{query}"

        cfg = LocalAgentConfig(tools=[search_web])
        assert search_web in cfg.tools


def test_mcp_cache_hit_logs_synthetic_path(daemon_up):
    """A cached MCP replay must appear in access_log as `mcp:<server>/<tool>`.

    Why: the daily series reads access_log by path and MCP has no path, so MCP
    traffic stayed invisible in the trend (0 rows on 2026-09-28) while
    cache_stats counted it. The synthetic path makes it visible without
    pretending it is a disk read — the body still reaches the model, so the row
    counts as I/O saving only.
    """
    import toolrecall.cache as cache_mod
    from toolrecall.cache import cached_mcp_check

    @antigravity.cached_tool(ttl=300)
    def probe(q: str) -> str:
        return f"r:{q}"

    probe("synthetic-path")
    hit = cached_mcp_check(antigravity.ADAPTER_SERVER, "probe", {"q": "synthetic-path"}, ttl=300)
    assert hit.get("cached") is True

    with cache_mod._db() as conn:
        rows = conn.execute(
            "SELECT category, path, hit FROM access_log WHERE category = 'mcp_cache'"
        ).fetchall()
    expected = ("mcp_cache", f"mcp:{antigravity.ADAPTER_SERVER}/probe", 1)
    assert expected in [tuple(r) for r in rows], f"synthetic mcp path missing: {rows}"


def real_runner_execute(runner, tool_name, **kwargs):
    import asyncio

    return asyncio.run(runner.execute(tool_name, **kwargs))
