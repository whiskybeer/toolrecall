"""Tests for the LangChain tool cache (toolrecall.adapters.langchain).

cached_tool() wraps a LangChain BaseTool so repeat calls with the same input
are served from ToolRecall's cache. Runs against the hermetic per-test cache
DB (conftest.py `_isolated_toolrecall_env`) with daemon_running() mocked, so
cached_mcp_check/store exercise the real cache code path.

Regression: the old ToolRecallCallbackHandler keyed results by the tool's
OUTPUT and nothing ever looked them up, so no tool call was ever saved.
"""

import asyncio
import uuid

import pytest

pytest.importorskip("langchain_core", reason="langchain-core not installed")

from langchain_core.tools import StructuredTool, tool  # noqa: E402

import toolrecall.client as tr_client  # noqa: E402
from toolrecall.adapters import langchain as lc  # noqa: E402


# ---- Fixtures ---------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_db_singleton(_isolated_toolrecall_env):
    """Make conftest's per-test TOOLRECALL_CACHE_DB actually take effect."""
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


def _counting_tool(name="lookup"):
    calls = []

    def lookup(query: str) -> str:
        """Look something up."""
        calls.append(query)
        return f"result:{query}"

    return StructuredTool.from_function(lookup, name=name), calls


# ---- cached_tool ------------------------------------------------------------


class TestCachedTool:
    def test_same_input_served_from_cache(self, daemon_up):
        t, calls = _counting_tool()
        ct = lc.cached_tool(t, ttl=300)

        assert ct.invoke({"query": "a"}) == "result:a"
        assert ct.invoke({"query": "a"}) == "result:a"
        assert calls == ["a"], "tool body must run once; second call is a cache hit"

    def test_string_input_served_from_cache(self, daemon_up):
        calls = []

        @tool
        def shout(text: str) -> str:
            """Upper-case text."""
            calls.append(text)
            return text.upper()

        ct = lc.cached_tool(shout, ttl=300)
        assert ct.invoke("hi") == "HI"
        assert ct.invoke("hi") == "HI"
        assert calls == ["hi"]

    def test_different_input_misses(self, daemon_up):
        t, calls = _counting_tool()
        ct = lc.cached_tool(t, ttl=300)

        assert ct.invoke({"query": "a"}) == "result:a"
        assert ct.invoke({"query": "b"}) == "result:b"
        assert calls == ["a", "b"]

    def test_key_is_input_not_output(self, daemon_up):
        """Two inputs with the SAME output must not share an entry, and the
        stored key must be derived from the input."""
        calls = []

        def const(query: str) -> str:
            """Constant output."""
            calls.append(query)
            return "same"

        ct = lc.cached_tool(StructuredTool.from_function(const, name="const"), ttl=300)
        ct.invoke({"query": "x"})
        ct.invoke({"query": "y"})
        assert calls == ["x", "y"], "identical output must not make a different input hit"

        from toolrecall.cache import cached_mcp_check

        by_input = cached_mcp_check(lc.ADAPTER_SERVER, "const", {"kwargs": {"query": "x"}})
        assert by_input["cached"] and by_input["data"] == '"same"'
        by_output = cached_mcp_check(lc.ADAPTER_SERVER, "const", {"input": "same"})
        assert not by_output["cached"]

    def test_user_arg_named_config_is_part_of_key(self, daemon_up):
        """Only the RunnableConfig-typed parameter is dropped from the key, not
        any argument that happens to be called "config"."""
        from langchain_core.tools import BaseTool

        calls = []

        class Deploy(BaseTool):
            name: str = "deploy"
            description: str = "Deploy a config."

            def _run(self, config: str) -> str:
                calls.append(config)
                return f"deployed:{config}"

        ct = lc.cached_tool(Deploy(), ttl=300)
        assert ct.invoke({"config": "a"}) == "deployed:a"
        assert ct.invoke({"config": "b"}) == "deployed:b"
        assert ct.invoke({"config": "a"}) == "deployed:a"
        assert calls == ["a", "b"]

    def test_runnable_config_param_not_part_of_key(self, daemon_up):
        from langchain_core.runnables import RunnableConfig

        calls = []

        def tagged(q: str, config: RunnableConfig) -> str:
            """Takes an injected RunnableConfig."""
            calls.append(q)
            return f"tagged:{q}"

        ct = lc.cached_tool(StructuredTool.from_function(tagged, name="tagged"), ttl=300)
        assert ct.invoke({"q": "a"}, config={"tags": ["one"]}) == "tagged:a"
        assert ct.invoke({"q": "a"}, config={"tags": ["two"]}) == "tagged:a"
        assert calls == ["a"]

    def test_tool_name_isolates_entries(self, daemon_up):
        t1, calls1 = _counting_tool("tool_one")
        t2, calls2 = _counting_tool("tool_two")
        lc.cached_tool(t1, ttl=300).invoke({"query": "a"})
        lc.cached_tool(t2, ttl=300).invoke({"query": "a"})
        assert calls1 == ["a"] and calls2 == ["a"]

    def test_original_tool_not_modified(self, daemon_up):
        t, calls = _counting_tool()
        lc.cached_tool(t, ttl=300).invoke({"query": "a"})
        t.invoke({"query": "a"})
        assert calls == ["a", "a"], "the unwrapped tool keeps running live"
        assert "_run" not in t.__dict__

    def test_tool_call_hit_still_returns_tool_message(self, daemon_up):
        t, calls = _counting_tool()
        ct = lc.cached_tool(t, ttl=300)
        call = {"type": "tool_call", "id": "c1", "name": "lookup", "args": {"query": "a"}}
        ct.invoke(call)
        msg = ct.invoke({**call, "id": "c2"})
        assert calls == ["a"]
        assert msg.content == "result:a"
        assert msg.tool_call_id == "c2"

    def test_callbacks_fire_on_hit(self, daemon_up):
        from langchain_core.callbacks import BaseCallbackHandler

        class Recorder(BaseCallbackHandler):
            def __init__(self):
                self.ends = []

            def on_tool_end(self, output, **kwargs):
                self.ends.append(output)

        t, calls = _counting_tool()
        ct = lc.cached_tool(t, ttl=300)
        rec = Recorder()
        ct.invoke({"query": "a"}, config={"callbacks": [rec]})
        ct.invoke({"query": "a"}, config={"callbacks": [rec]})
        assert calls == ["a"]
        assert rec.ends == ["result:a", "result:a"]

    def test_json_dict_result_round_trips(self, daemon_up):
        calls = []

        def info(key: str) -> dict:
            """Return a dict."""
            calls.append(key)
            return {"key": key, "n": 1}

        ct = lc.cached_tool(StructuredTool.from_function(info, name="info"), ttl=300)
        assert ct.invoke({"key": "k"}) == {"key": "k", "n": 1}
        assert ct.invoke({"key": "k"}) == {"key": "k", "n": 1}
        assert calls == ["k"]

    def test_content_and_artifact_restored_as_tuple(self, daemon_up):
        calls = []

        def fetch(q: str):
            """Content + artifact."""
            calls.append(q)
            return f"content:{q}", {"raw": q}

        ct = lc.cached_tool(
            StructuredTool.from_function(
                fetch, name="fetch", response_format="content_and_artifact"
            ),
            ttl=300,
        )
        call = {"type": "tool_call", "id": "c1", "name": "fetch", "args": {"q": "z"}}
        ct.invoke(call)
        msg = ct.invoke({**call, "id": "c2"})
        assert calls == ["z"]
        assert msg.content == "content:z"
        assert msg.artifact == {"raw": "z"}

    def test_non_serializable_result_not_cached(self, daemon_up):
        calls = []

        def obj(q: str) -> object:
            """Returns an object."""
            calls.append(q)
            return {"o": object()}

        ct = lc.cached_tool(StructuredTool.from_function(obj, name="obj"), ttl=300)
        ct.invoke({"q": "a"})
        ct.invoke({"q": "a"})
        assert calls == ["a", "a"]

    def test_errors_not_cached(self, daemon_up):
        calls = []

        def flaky(q: str) -> str:
            """Fails first."""
            calls.append(q)
            if len(calls) == 1:
                raise RuntimeError("boom")
            return "ok"

        ct = lc.cached_tool(StructuredTool.from_function(flaky, name="flaky"), ttl=300)
        with pytest.raises(RuntimeError):
            ct.invoke({"q": "a"})
        assert ct.invoke({"q": "a"}) == "ok"
        assert ct.invoke({"q": "a"}) == "ok"
        assert calls == ["a", "a"]

    def test_ttl_zero_bypasses(self, daemon_up):
        t, calls = _counting_tool()
        ct = lc.cached_tool(t, ttl=0)
        ct.invoke({"query": "a"})
        ct.invoke({"query": "a"})
        assert calls == ["a", "a"]

    def test_daemon_down_runs_live(self, daemon_down, monkeypatch):
        def fail(*a, **k):
            raise AssertionError("cache must not be touched when the daemon is down")

        monkeypatch.setattr(lc, "cached_mcp_check", fail)
        monkeypatch.setattr(lc, "cached_mcp_store", fail)
        t, calls = _counting_tool()
        ct = lc.cached_tool(t, ttl=300)
        assert ct.invoke({"query": "a"}) == "result:a"
        assert ct.invoke({"query": "a"}) == "result:a"
        assert calls == ["a", "a"]

    def test_cache_check_error_does_not_break_tool(self, daemon_up, monkeypatch):
        def broken(*a, **k):
            raise RuntimeError("db gone")

        monkeypatch.setattr(lc, "cached_mcp_check", broken)
        t, calls = _counting_tool()
        ct = lc.cached_tool(t, ttl=300)
        assert ct.invoke({"query": "a"}) == "result:a"
        assert calls == ["a"]

    def test_cache_store_error_does_not_break_tool(self, daemon_up, monkeypatch):
        def broken(*a, **k):
            raise RuntimeError("disk full")

        monkeypatch.setattr(lc, "cached_mcp_store", broken)
        t, calls = _counting_tool()
        ct = lc.cached_tool(t, ttl=300)
        assert ct.invoke({"query": "a"}) == "result:a"
        assert ct.invoke({"query": "a"}) == "result:a"
        assert calls == ["a", "a"]

    def test_corrupt_cached_data_is_a_miss(self, daemon_up, monkeypatch):
        monkeypatch.setattr(
            lc, "cached_mcp_check", lambda *a, **k: {"cached": True, "data": "{not json", "key": ""}
        )
        monkeypatch.setattr(lc, "cached_mcp_store", lambda *a, **k: None)
        t, calls = _counting_tool()
        assert lc.cached_tool(t, ttl=300).invoke({"query": "a"}) == "result:a"
        assert calls == ["a"]

    def test_async_default_arun_served_from_cache(self, daemon_up):
        t, calls = _counting_tool()
        ct = lc.cached_tool(t, ttl=300)

        async def go():
            return [await ct.ainvoke({"query": "a"}), await ct.ainvoke({"query": "a"})]

        assert asyncio.run(go()) == ["result:a", "result:a"]
        assert calls == ["a"]

    def test_async_coroutine_tool_served_from_cache(self, daemon_up):
        calls = []

        async def alookup(query: str) -> str:
            """Async lookup."""
            calls.append(query)
            return f"async:{query}"

        ct = lc.cached_tool(StructuredTool.from_function(coroutine=alookup, name="alookup"))

        async def go():
            return [await ct.ainvoke({"query": "a"}), await ct.ainvoke({"query": "a"})]

        assert asyncio.run(go()) == ["async:a", "async:a"]
        assert calls == ["a"]

    def test_sync_and_async_share_entries(self, daemon_up):
        t, calls = _counting_tool()
        ct = lc.cached_tool(t, ttl=300)
        ct.invoke({"query": "a"})
        assert asyncio.run(ct.ainvoke({"query": "a"})) == "result:a"
        assert calls == ["a"]

    def test_rejects_non_tool(self):
        with pytest.raises(TypeError):
            lc.cached_tool(lambda q: q)


# ---- ToolRecallCallbackHandler (store-only) ----------------------------------


class TestCallbackHandlerStoreOnly:
    def test_records_by_input_and_cached_tool_serves_it(self, daemon_up):
        handler = lc.ToolRecallCallbackHandler(ttl=300)
        rid = uuid.uuid4()
        handler.on_tool_start(
            {"name": "lookup"}, "{'query': 'a'}", run_id=rid, inputs={"query": "a"}
        )
        handler.on_tool_end("recorded:a", run_id=rid, name="lookup")

        t, calls = _counting_tool()
        assert lc.cached_tool(t, ttl=300).invoke({"query": "a"}) == "recorded:a"
        assert calls == []

    def test_string_input_recorded_by_input(self, daemon_up):
        from toolrecall.cache import cached_mcp_check

        handler = lc.ToolRecallCallbackHandler(ttl=300)
        rid = uuid.uuid4()
        handler.on_tool_start({"name": "echo"}, "in", run_id=rid)
        handler.on_tool_end("out", run_id=rid, name="echo")

        assert cached_mcp_check(lc.ADAPTER_SERVER, "echo", {"input": "in"})["cached"]
        assert not cached_mcp_check(lc.ADAPTER_SERVER, "echo", {"input": "out"})["cached"]

    def test_error_run_not_recorded(self, daemon_up):
        from toolrecall.cache import cached_mcp_check

        handler = lc.ToolRecallCallbackHandler(ttl=300)
        rid = uuid.uuid4()
        handler.on_tool_start({"name": "echo"}, "in", run_id=rid)
        handler.on_tool_error(ValueError("x"), run_id=rid, name="echo")
        handler.on_tool_end("late", run_id=rid, name="echo")

        assert not cached_mcp_check(lc.ADAPTER_SERVER, "echo", {"input": "in"})["cached"]
        assert "echo" in handler._tool_errors

    def test_store_error_does_not_raise(self, daemon_up, monkeypatch):
        def broken(*a, **k):
            raise RuntimeError("db gone")

        monkeypatch.setattr(lc, "cached_mcp_check", broken)
        handler = lc.ToolRecallCallbackHandler()
        handler.on_tool_start({"name": "echo"}, "in", run_id=1)
        handler.on_tool_end("out", run_id=1, name="echo")

    def test_bound_handler_in_real_tool_run(self, daemon_up):
        lc._ensure_base()
        handler = lc.ToolRecallCallbackHandler(ttl=300)
        t, calls = _counting_tool()
        t.invoke({"query": "a"}, config={"callbacks": [handler]})
        t.invoke({"query": "a"}, config={"callbacks": [handler]})
        assert calls == ["a", "a"], "a callback handler cannot skip execution"
        assert lc.cached_tool(t, ttl=300).invoke({"query": "a"}) == "result:a"
        assert calls == ["a", "a"], "but its recorded result is served by cached_tool"
