"""Tests for the LangGraph adapter (toolrecall.adapters.langgraph).

Two layers:
- Framework-free core (_SaverCore, cached_tool, _StdlibSerde): runs everywhere,
  against the hermetic per-test cache DB (conftest.py), with daemon_running()
  mocked True so cached_mcp_check/store exercise the real cache code path.
- LangGraph binding (get_saver + real StateGraph execution): skipped unless
  langgraph is importable (mirrors test_e2e_langchain_adapter conventions).
"""

import pytest

import toolrecall.client as tr_client


def langgraph_mod():
    from toolrecall.adapters import langgraph as mod

    return mod


# ---- Fixtures ---------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_db_singleton(_isolated_toolrecall_env):
    """Make conftest's per-test TOOLRECALL_CACHE_DB actually take effect.

    toolrecall._db caches both config and the sqlite connection as module
    singletons; without this reset every test after the first writes to the
    FIRST test's temp DB (verified: delete_thread_rows saw another test's 5
    rows). Test-infra only — no production code touched.
    """
    import toolrecall._db as dbmod

    dbmod._cached_config = None
    if getattr(dbmod, "_db_real", None) is not None:
        try:
            dbmod._db_real.close()
        except Exception:
            pass
        dbmod._db_real = None
    # Re-bootstrap schema on the fresh DB (same as a fresh process would).
    from toolrecall.cache import SCHEMA

    dbmod._init(SCHEMA)
    yield


@pytest.fixture()
def daemon_up(monkeypatch):
    """Simulate a running daemon; cache.py writes to the hermetic test DB."""
    monkeypatch.setattr(tr_client, "daemon_running", lambda: True)


@pytest.fixture()
def daemon_down(monkeypatch):
    monkeypatch.setattr(tr_client, "daemon_running", lambda: False)


# ---- Module shape -------------------------------------------------------------


class TestModuleShape:
    def test_import_without_langgraph(self):
        mod = langgraph_mod()
        assert mod.ADAPTER_SERVER == "langgraph"
        assert callable(mod.cached_tool)
        assert callable(mod.ToolRecallCheckpointSaver)

    def test_stdlib_serde_roundtrip(self):
        mod = langgraph_mod()
        s = mod._StdlibSerde()
        t, blob = s.dumps_typed({"a": 1, "b": [1, 2]})
        assert t == "json"
        assert s.loads_typed((t, blob)) == {"a": 1, "b": [1, 2]}

    def test_stdlib_serde_rejects_non_json(self):
        mod = langgraph_mod()
        s = mod._StdlibSerde()
        with pytest.raises(TypeError):
            s.dumps_typed(object())


# ---- cached_tool ---------------------------------------------------------------


class TestCachedTool:
    def test_daemon_down_executes_live(self, daemon_down):
        mod = langgraph_mod()
        calls = []

        @mod.cached_tool
        def tool(x):
            calls.append(x)
            return x * 2

        assert tool(1) == 2
        assert tool(1) == 2
        assert len(calls) == 2  # no caching without daemon

    def test_hit_after_miss(self, daemon_up):
        mod = langgraph_mod()
        calls = []

        @mod.cached_tool(ttl=300)
        def tool(x):
            calls.append(x)
            return {"result": x * 2}

        assert tool(21) == {"result": 42}
        assert tool(21) == {"result": 42}
        assert len(calls) == 1  # second call served from cache

    def test_different_args_different_entry(self, daemon_up):
        mod = langgraph_mod()
        calls = []

        @mod.cached_tool(ttl=300)
        def tool(x):
            calls.append(x)
            return x

        assert tool(1) == 1
        assert tool(2) == 2
        assert len(calls) == 2

    def test_async_tool_cached(self, daemon_up):
        import asyncio

        mod = langgraph_mod()
        calls = []

        @mod.cached_tool(ttl=300)
        async def atool(x):
            calls.append(x)
            return x + 1

        assert asyncio.run(atool(5)) == 6
        assert asyncio.run(atool(5)) == 6
        assert len(calls) == 1

    def test_non_json_result_is_serialized_by_repr(self, daemon_up):
        mod = langgraph_mod()
        calls = []

        @mod.cached_tool(ttl=300)
        def tool(x):
            calls.append(x)
            return object()

        a = tool(1)
        b = tool(1)
        # Documented limitation: non-JSON results are stored via repr(),
        # so the cached hit returns the STRING, not a fresh object.
        assert isinstance(a, object)
        assert isinstance(b, str)
        assert len(calls) == 1

    def test_name_override(self, daemon_up):
        mod = langgraph_mod()

        @mod.cached_tool(ttl=300, name="custom_tool")
        def tool(x):
            return x

        assert tool("k") == "k"


# ---- _SaverCore (framework-free) ------------------------------------------------


@pytest.fixture()
def core(daemon_up):
    mod = langgraph_mod()
    return mod._SaverCore(ttl=300, serde=mod._StdlibSerde())


class TestSaverCore:
    def test_put_get_roundtrip(self, core):
        ckpt = {"id": "c1", "v": 1, "channel_values": {"x": 42}}
        meta = {"source": "loop", "step": 1}
        core.put("t1", "", ckpt, meta, None)
        got = core.get("t1", "", "c1")
        assert got is not None
        assert got["checkpoint"]["channel_values"]["x"] == 42
        assert got["metadata"]["source"] == "loop"
        assert got["parent"] is None
        assert got["checkpoint_id"] == "c1"

    def test_get_latest_when_no_id(self, core):
        for i in range(3):
            core.put("t1", "", {"id": f"c{i}"}, {}, None)
        got = core.get("t1", "", None)
        assert got["checkpoint_id"] == "c2"

    def test_get_unknown_thread(self, core):
        assert core.get("nope", "", None) is None

    def test_parent_preserved(self, core):
        core.put("t1", "", {"id": "c1"}, {}, {"id": "p1"})
        got = core.get("t1", "", "c1")
        assert got["parent"]["id"] == "p1"

    def test_index_order_and_limit(self, core):
        for i in range(5):
            core.put("t1", "", {"id": f"c{i}"}, {}, None)
        assert core.list_ids("t1", "") == [f"c{i}" for i in range(5)]
        assert core.list_ids("t1", "", limit=2) == ["c3", "c4"]

    def test_pending_writes_roundtrip(self, core):
        core.put("t1", "", {"id": "c1"}, {}, None)
        core.put_pending_writes("t1", "", "c1", [["chan", {"deep": [1, 2]}]], "task1")
        core.put_pending_writes("t1", "", "c1", [["chan", "v2"]], "task2")
        got = core.get("t1", "", "c1")
        assert [p["task_id"] for p in got["pending"]] == ["task1", "task2"]

    def test_pending_writes_missing_checkpoint(self, core):
        # must not raise
        core.put_pending_writes("tX", "", "missing", [["c", "v"]], "t")

    def test_delete_thread_clears_index(self, core):
        core.put("t1", "", {"id": "c1"}, {}, None)
        n = core.delete_thread_rows("t1", "")
        assert n == 1
        # Contract: delete is logical — index cleared, so latest/list reads
        # see nothing. Rows themselves age out via TTL (mcp_cache has no
        # per-key delete in v1), so an explicit-id read may still serve
        # stale data until expiry — asserted here to pin the behavior.
        assert core.get("t1", "", None) is None
        assert core.list_ids("t1", "") == []
        assert core.get("t1", "", "c1") is not None

    def test_corrupt_row_returns_none(self, core, monkeypatch):
        monkeypatch.setattr(core, "_check", lambda key: "{not json")
        assert core.get("t1", "", "c1") is None

    def test_row_key_format(self, core):
        assert core._row_key("thr", "ns", "cid") == "lg:thr:ns:cid"
        assert core._index_key("thr", "ns") == "lgidx:thr:ns"

    def test_serde_failure_raises_not_silently_degrades(self, core):
        with pytest.raises(TypeError):
            core.build_row(object(), {}, None, "[]")


# ---- LangGraph binding (requires langgraph) -------------------------------------


class TestLangGraphBinding:
    def test_get_saver_is_base_checkpoint_saver(self, daemon_up):
        pytest.importorskip("langgraph")
        from langgraph.checkpoint.base import BaseCheckpointSaver

        saver = langgraph_mod().get_saver()
        assert isinstance(saver, BaseCheckpointSaver)

    def test_saver_protocol_roundtrip(self, daemon_up):
        pytest.importorskip("langgraph")
        mod = langgraph_mod()
        saver = mod.get_saver(ttl=300)

        config = {"configurable": {"thread_id": "thr"}}
        chk = {
            "v": 1,
            "id": "ckpt-1",
            "ts": "2026-09-09T00:00:00",
            "channel_values": {"messages": ["hi"]},
            "channel_versions": {"messages": 1},
            "versions_seen": {},
            "updated_channels": ["messages"],
        }
        saver.put(config, chk, {"source": "input"}, {})
        tup = saver.get_tuple(config)
        assert tup is not None
        assert tup.checkpoint["id"] == "ckpt-1"
        assert tup.checkpoint["channel_values"]["messages"] == ["hi"]
        assert tup.metadata["source"] == "input"

        # history ordering, oldest first
        saver.put(config, dict(chk, id="ckpt-2"), {"source": "loop"}, {})
        hist = list(saver.list(config))
        assert [t.checkpoint["id"] for t in hist] == ["ckpt-1", "ckpt-2"]

        # fork: get_tuple at the older checkpoint restores exactly it
        old = saver.get_tuple({"configurable": {"thread_id": "thr", "checkpoint_id": "ckpt-1"}})
        assert old.checkpoint["id"] == "ckpt-1"
        assert old.parent_config is None

        # second checkpoint's parent_config points at the first
        newer = saver.get_tuple({"configurable": {"thread_id": "thr", "checkpoint_id": "ckpt-2"}})
        assert newer.parent_config["configurable"]["checkpoint_id"] == "ckpt-1"

        # pending writes round-trip through serde
        saver.put_writes(
            {"configurable": {"thread_id": "thr", "checkpoint_id": "ckpt-1"}},
            [("messages", {"interrupt": True})],
            "task-abc",
        )
        tup_old = saver.get_tuple({"configurable": {"thread_id": "thr", "checkpoint_id": "ckpt-1"}})
        assert ("messages", {"interrupt": True}) in tup_old.pending_writes

        # delete_thread clears history
        saver.delete_thread("thr")
        assert list(saver.list(config)) == []

    def test_saver_daemon_down_graceful(self, daemon_down):
        pytest.importorskip("langgraph")
        saver = langgraph_mod().get_saver()
        config = {"configurable": {"thread_id": "thr"}}
        chk = {
            "v": 1,
            "id": "c1",
            "ts": "t",
            "channel_values": {},
            "channel_versions": {},
            "versions_seen": {},
        }
        saver.put(config, chk, {}, {})  # dropped silently
        assert saver.get_tuple(config) is None


# ---- Real graph end-to-end (requires langgraph) ---------------------------------


class TestRealGraph:
    def test_graph_fork_resume_with_cached_tool(self, daemon_up):
        pytest.importorskip("langgraph")
        from typing_extensions import TypedDict

        from langgraph.graph import END, StateGraph

        mod = langgraph_mod()

        calls = []

        @mod.cached_tool(ttl=300, name="fetch_inner")
        def fetch_inner(q: str) -> str:
            calls.append(q)
            return q.upper()

        class S(TypedDict):
            messages: list

        def node_fetch(state: S) -> dict:
            return {"messages": [fetch_inner(state["messages"][-1])]}

        g = StateGraph(S)
        g.add_node("fetch", node_fetch)
        g.set_entry_point("fetch")
        g.add_edge("fetch", END)
        graph = g.compile(checkpointer=mod.get_saver(ttl=300))

        cfg = {"configurable": {"thread_id": "graph-thr"}}
        out1 = graph.invoke({"messages": ["hello"]}, config=cfg)
        assert out1["messages"][-1] == "HELLO"
        assert len(calls) == 1

        # Same input, fresh thread: tool cache serves the node work.
        cfg2 = {"configurable": {"thread_id": "graph-thr-2"}}
        out2 = graph.invoke({"messages": ["hello"]}, config=cfg2)
        assert out2["messages"][-1] == "HELLO"
        assert len(calls) == 1  # tool ran once across both threads

        # Checkpoints exist per thread and are forkable by id.
        saver = mod.get_saver(ttl=300)
        hist = list(saver.list({"configurable": {"thread_id": "graph-thr"}}))
        assert len(hist) >= 1
        assert hist[0].checkpoint["id"]
