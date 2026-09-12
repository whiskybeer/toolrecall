"""E2E: LangGraph adapter — REAL langgraph mechanics, in-process hermetic DB.

Adapter reality (verified 2026-09-10, see toolrecall-adapter-authoring skill):
toolrecall adapters call the cache IN-PROCESS via toolrecall.cache (direct
SQLite through toolrecall._db) — they never talk to the daemon socket. A
spawned daemon is decorative for this path, and an unset TOOLRECALL_CACHE_DB
points the adapter at the PRODUCTION ~/.toolrecall/cache.db. So this module
sets a hermetic per-test DB before any toolrecall import and never spawns a
daemon.

The five scenarios mirror tests/run_langgraph_e2e_standalone.py exactly (the
standalone runner is the authoritative executor — pytest's interpreter
shutdown can hang joining langgraph's executor threads; the runner uses
per-scenario watchdogs + os._exit). Both must stay in sync.

Requires langgraph (pip install toolrecall[langgraph]); skips otherwise.
Unit-level suite: tests/test_langgraph_adapter.py (framework-free core runs
everywhere; binding tests skip without langgraph).
"""

import itertools
import os
import sys
import tempfile
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Hermetic cache DB + no autostart BEFORE any toolrecall import (the _db
# singleton resolves the path at first use; conftest's production-path guard
# is the seatbelt — without this block the adapter would touch
# ~/.toolrecall/cache.db while the live daemon holds it).
os.environ["TOOLRECALL_CACHE_DB"] = tempfile.mktemp(prefix="lg_e2e_", suffix=".db")
os.environ["TOOLRECALL_NO_AUTOSTART"] = "1"
os.environ["TOOLRECALL_DOCS_INDEX_TTL"] = "0"
os.environ["TOOLRECALL_UPDATE_CHECK"] = "0"

import pytest

pytest.importorskip("langgraph", reason="langgraph not installed")
pytest.importorskip("langchain_core", reason="langchain-core not installed")

pytestmark = pytest.mark.e2e


@pytest.fixture(autouse=True)
def _reset_db_singleton(_isolated_toolrecall_env):
    """Make conftest's per-test TOOLRECALL_CACHE_DB actually take effect.

    toolrecall._db caches both config and the sqlite connection as module
    singletons; without this reset every test after the first writes to the
    FIRST test's temp DB (verified: delete_thread_rows saw another test's 5
    rows). Re-bootstraps the schema on the fresh DB. Test-infra only.
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
    """Adapters gate the cache on client.daemon_running() — mock UP."""
    import toolrecall.client as tr_client

    monkeypatch.setattr(tr_client, "daemon_running", lambda: True)


def langgraph_mod():
    import toolrecall.adapters.langgraph as mod

    return mod


def _scripted_model(script):
    """Fake chat model: LAZY cycle (toolrecall-adapter-authoring skill —
    materializing an infinite iterator inside a graph node hangs forever)."""
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel

    return GenericFakeChatModel(messages=itertools.cycle(list(script)))


def _make_fetch_graph(saver, tag):
    """fetch(cached_tool) -> chat(fake model). Returns (graph, calls).

    The tag makes tool args unique per scenario: scenarios share one
    hermetic DB in-process, so identical args make scenario N's fetch a
    cache HIT from scenario N-1 (breaks ran-once assertions).
    """
    from langchain_core.messages import AIMessage
    from langgraph.graph import END, START, MessagesState, StateGraph

    from toolrecall.adapters.langgraph import cached_tool

    calls = []

    @cached_tool(ttl=600, name="e2e_fetch")
    def e2e_fetch(q: str) -> dict:
        calls.append(q)
        return {"data": f"fetched:{q}"}

    def fetch_node(state):
        out = e2e_fetch(state["messages"][-1].content + tag)
        return {"messages": [AIMessage(content=f"F[{out['data']}]")]}

    def chat_node(state):
        resp = _scripted_model([AIMessage(content="done")]).invoke(list(state["messages"]))
        return {"messages": [resp]}

    g = StateGraph(MessagesState)
    g.add_node("fetch", fetch_node)
    g.add_node("chat", chat_node)
    g.add_edge(START, "fetch")
    g.add_edge("fetch", "chat")
    g.add_edge("chat", END)
    return g.compile(checkpointer=saver), calls


def test_multi_node_run_history_parents(daemon_up):
    from langchain_core.messages import HumanMessage

    mod = langgraph_mod()
    saver = mod.get_saver(ttl=600)
    graph, calls = _make_fetch_graph(saver, tag="mn")
    cfg = {"configurable": {"thread_id": "main"}}
    out = graph.invoke({"messages": [HumanMessage(content="hello")]}, config=cfg)
    assert [m.content for m in out["messages"]][-1] == "done"
    assert len(calls) == 1

    hist = list(graph.get_state_history(cfg))
    ids = [h.config["configurable"]["checkpoint_id"] for h in hist]
    assert len(hist) >= 3, f"history too short: {len(hist)}"
    assert len(set(ids)) == len(ids), f"duplicate checkpoint ids: {ids}"
    step, parents = hist[0], 0
    while step.parent_config is not None:
        parents += 1
        nxt = graph.get_state(step.parent_config)
        if (
            nxt is None
            or nxt.config["configurable"]["checkpoint_id"]
            == step.config["configurable"]["checkpoint_id"]
        ):
            break
        step = nxt
    assert parents >= 2, f"parent chain too short: walked {parents} hops"


def test_interrupt_and_resume(daemon_up):
    from langchain_core.messages import AIMessage, HumanMessage
    from langgraph.graph import END, START, MessagesState, StateGraph
    from langgraph.types import Command, interrupt

    from toolrecall.adapters.langgraph import cached_tool

    mod = langgraph_mod()
    saver = mod.get_saver(ttl=600)
    calls = []
    tag = f"#{id(saver) % 100000}"

    @cached_tool(ttl=600, name="e2e_fetch")
    def e2e_fetch(q: str) -> dict:
        calls.append(q)
        return {"data": f"fetched:{q}"}

    def fetch_node(state):
        out = e2e_fetch(state["messages"][-1].content + tag)
        return {"messages": [AIMessage(content=f"F[{out['data']}]")]}

    def review_node(state):
        answer = interrupt({"question": "approve?"})  # parks the graph
        return {"messages": [AIMessage(content=f"R[{answer}]")]}

    def chat_node(state):
        resp = _scripted_model([AIMessage(content="resumed!")]).invoke(list(state["messages"]))
        return {"messages": [resp]}

    builder = StateGraph(MessagesState)
    builder.add_node("fetch", fetch_node)
    builder.add_node("review", review_node)
    builder.add_node("chat", chat_node)
    builder.add_edge(START, "fetch")
    builder.add_edge("fetch", "review")
    builder.add_edge("review", "chat")
    builder.add_edge("chat", END)
    graph = builder.compile(checkpointer=saver)

    cfg = {"configurable": {"thread_id": "irq"}}
    graph.invoke({"messages": [HumanMessage(content="hello")]}, config=cfg)
    state = graph.get_state(cfg)
    assert state.next, f"graph must park mid-flow, next={state.next}"
    assert len(calls) == 1

    out2 = graph.invoke(Command(resume="ok"), config=cfg)
    contents = [m.content for m in out2["messages"]]
    assert any(c.startswith("R[") for c in contents), contents
    assert contents[-1] == "resumed!"
    assert len(calls) == 1, f"fetch re-ran after resume: {calls}"


def test_time_travel_fork_tool_cached_once(daemon_up):
    from langchain_core.messages import HumanMessage

    mod = langgraph_mod()
    saver = mod.get_saver(ttl=600)
    graph, calls = _make_fetch_graph(saver, tag=f"tt-{uuid.uuid4().hex[:8]}")
    cfg = {"configurable": {"thread_id": f"tt-{uuid.uuid4().hex[:8]}"}}
    graph.invoke({"messages": [HumanMessage(content="hello")]}, config=cfg)
    assert len(calls) == 1, f"first run tool calls: {calls}"

    hist = list(graph.get_state_history(cfg))
    post_fetch = None
    for h in hist:
        tail = [m.content for m in h.values["messages"]][-1]
        if tail.startswith("F["):
            post_fetch = h
            break
    assert post_fetch is not None, "no post-fetch checkpoint in history"
    target_id = post_fetch.config["configurable"]["checkpoint_id"]
    tid = cfg["configurable"]["thread_id"]

    rewound = graph.get_state({"configurable": {"thread_id": tid, "checkpoint_id": target_id}})
    assert [m.content for m in rewound.values["messages"]][-1].startswith("F[")

    out2 = graph.invoke(
        None, config={"configurable": {"thread_id": tid, "checkpoint_id": target_id}}
    )
    assert len(calls) == 1, f"tool re-ran on rewind: {calls}"
    assert [m.content for m in out2["messages"]][-1] == "done"


def test_persistence_across_saver_instances(daemon_up):
    from langchain_core.messages import HumanMessage

    mod = langgraph_mod()
    saver = mod.get_saver(ttl=600)
    graph, _ = _make_fetch_graph(saver, tag=f"p-{uuid.uuid4().hex[:8]}")
    cfg = {"configurable": {"thread_id": f"persist-{uuid.uuid4().hex[:8]}"}}
    graph.invoke({"messages": [HumanMessage(content="hello")]}, config=cfg)

    fresh = mod.get_saver(ttl=600)  # separate instance, same DB
    graph2, _ = _make_fetch_graph(fresh, tag="ignored")
    state = graph2.get_state(cfg)
    assert [m.content for m in state.values["messages"]][-1] == "done"


def test_subgraph_namespace(daemon_up):
    from langchain_core.messages import AIMessage, HumanMessage
    from langgraph.graph import END, START, StateGraph

    mod = langgraph_mod()
    saver = mod.get_saver(ttl=600)
    cfg = {"configurable": {"thread_id": f"sub-{uuid.uuid4().hex[:8]}"}}

    child = StateGraph(dict)
    child.add_node("echo", lambda s: {"messages": [AIMessage(content="child-done")]})
    child.add_edge(START, "echo")
    child.add_edge("echo", END)
    child_compiled = child.compile(checkpointer=saver)

    parent = StateGraph(dict)
    parent.add_node("sub", child_compiled)
    parent.add_edge(START, "sub")
    parent.add_edge("sub", END)
    parent_compiled = parent.compile(checkpointer=saver)

    out = parent_compiled.invoke({"messages": [HumanMessage(content="go")]}, config=cfg)
    assert [m.content for m in out["messages"]][-1] == "child-done"

    # Listing is per-namespace (same as SqliteSaver). The child namespace is
    # "<node>:<task_id>" — proven via history snapshots' task states.
    nss = {t.config["configurable"].get("checkpoint_ns", "") for t in saver.list(cfg)}
    assert "" in nss, nss
    child_ns = None
    for snap in parent_compiled.get_state_history(cfg):
        for task in snap.tasks:
            tstate = getattr(task, "state", None)
            tns = (
                tstate.get("configurable", {}).get("checkpoint_ns")
                if isinstance(tstate, dict)
                else None
            )
            if tns:
                child_ns = tns
                break
        if child_ns:
            break
    assert child_ns, "no subgraph namespace found in history tasks"
    child_snap = parent_compiled.get_state(
        {"configurable": {"thread_id": cfg["configurable"]["thread_id"], "checkpoint_ns": child_ns}}
    )
    child_tail = [m.content for m in child_snap.values["messages"]][-1]
    assert child_tail == "child-done", f"child state tail: {child_tail}"
