"""E2E: LangGraph adapter against a REAL ToolRecall daemon + real langgraph.

Spawns a real daemon subprocess (tests.e2e_helpers.E2EDaemon) on a hermetic
socket/DB and runs real LangGraph 1.x graphs against it:

- multi-node graph with langchain message objects as channel state (serde path)
- functional interrupt() + Command(resume=...) — real human-in-the-loop mechanics
- time-travel rewind to a mid-history checkpoint (same-thread rewind — the
  supported fork pattern; cross-thread forking needs state copy)
- fresh saver instance (simulated process restart) reading persisted state
- subgraph (checkpoint_ns) — per-namespace scoping through the real protocol
- cached_tool executed exactly once across rewind/replay runs

No LLM API calls: a scripted fake chat model drives the graph, so the suite
costs $0 while exercising the real saver protocol end to end.

Requires langgraph (skip if absent); runs in the throwaway venv against the
repo source tree.
"""

import itertools
import os
import sys
import threading

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

pytest.importorskip("langgraph")
pytest.importorskip("langchain_core")

pytestmark = pytest.mark.e2e

from tests.e2e_helpers import E2EDaemon  # noqa: E402


def _drain(pipe):
    """Drain a daemon pipe forever — unread stderr fills 64KB and the daemon
    blocks mid-write, deadlocking every subsequent cache call."""
    try:
        for _ in iter(pipe.readline, b""):
            pass
    except Exception:
        pass


@pytest.fixture()
def daemon():
    """Real daemon subprocess on a hermetic socket + DB."""
    d = E2EDaemon()
    d.start()
    # Drain the daemon's pipes in background threads (see _drain above);
    # pytest captures no daemon output, so without this the e2e deadlocks
    # once the daemon logs enough to fill the pipe buffer.
    threads = [
        threading.Thread(target=_drain, args=(d.process.stdout,), daemon=True),
        threading.Thread(target=_drain, args=(d.process.stderr,), daemon=True),
    ]
    for t in threads:
        t.start()
    # Point the in-process client at the spawned daemon BEFORE any adapter
    # cache call. set_socket_path() also refreshes transport.DEFAULT_PATH.
    import toolrecall.client as tr_client

    tr_client.set_socket_path(d.socket_path)
    original = tr_client.daemon_running
    tr_client.daemon_running = lambda: True  # socket is real; skip auto-probe
    yield d
    tr_client.daemon_running = original
    d.stop()


def _fresh_saver(ttl=600):
    from toolrecall.adapters.langgraph import get_saver

    return get_saver(ttl=ttl)


def _scripted_model(script):
    """Fake chat model: cycles the script forever (LAZY — never list() an
    infinite iterator; that was the original e2e hang)."""
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel

    return GenericFakeChatModel(messages=itertools.cycle(list(script)))


def _make_fetch_graph(saver, script):
    """fetch (cached_tool) → chat (scripted model). Returns (graph, calls)."""
    from langchain_core.messages import AIMessage
    from langgraph.graph import END, START, MessagesState, StateGraph

    from toolrecall.adapters.langgraph import cached_tool

    calls = []

    @cached_tool(ttl=600, name="e2e_fetch")
    def e2e_fetch(q: str) -> dict:
        calls.append(q)
        return {"data": f"fetched:{q}"}

    def fetch_node(state):
        q = state["messages"][-1].content
        out = e2e_fetch(q)
        return {"messages": [AIMessage(content=f"F[{out['data']}]")]}

    def chat_node(state):
        resp = _scripted_model(script).invoke(list(state["messages"]))
        return {"messages": [resp]}

    g = StateGraph(MessagesState)
    g.add_node("fetch", fetch_node)
    g.add_node("chat", chat_node)
    g.add_edge(START, "fetch")
    g.add_edge("fetch", "chat")
    g.add_edge("chat", END)
    return g.compile(checkpointer=saver), calls


def _make_interrupt_graph(saver, script):
    """fetch → review (functional interrupt) → chat. Returns (graph, calls)."""
    from langchain_core.messages import AIMessage
    from langgraph.graph import END, START, MessagesState, StateGraph
    from langgraph.types import interrupt

    from toolrecall.adapters.langgraph import cached_tool

    calls = []

    @cached_tool(ttl=600, name="e2e_fetch")
    def e2e_fetch(q: str) -> dict:
        calls.append(q)
        return {"data": f"fetched:{q}"}

    def fetch_node(state):
        out = e2e_fetch(state["messages"][-1].content)
        return {"messages": [AIMessage(content=f"F[{out['data']}]")]}

    def review_node(state):
        answer = interrupt({"question": "approve?"})  # parks the graph
        return {"messages": [AIMessage(content=f"R[{answer}]")]}

    def chat_node(state):
        resp = _scripted_model(script).invoke(list(state["messages"]))
        return {"messages": [resp]}

    g = StateGraph(MessagesState)
    g.add_node("fetch", fetch_node)
    g.add_node("review", review_node)
    g.add_node("chat", chat_node)
    g.add_edge(START, "fetch")
    g.add_edge("fetch", "review")
    g.add_edge("review", "chat")
    g.add_edge("chat", END)
    return g.compile(checkpointer=saver), calls


class TestRealGraphE2E:
    def test_multi_node_run_history_parents(self, daemon):
        """Real graph on a real daemon: run, inspect history + parent chain."""
        from langchain_core.messages import AIMessage, HumanMessage

        saver = _fresh_saver()
        graph, calls = _make_fetch_graph(saver, script=[AIMessage(content="done")])

        cfg = {"configurable": {"thread_id": "main"}}
        out = graph.invoke({"messages": [HumanMessage(content="hello")]}, config=cfg)
        assert [m.content for m in out["messages"]][-1] == "done"
        assert calls == ["hello"]  # tool ran exactly once

        hist = list(graph.get_state_history(cfg))
        assert len(hist) >= 3  # __start__ + fetch + chat super-steps
        ids = [h.config["configurable"]["checkpoint_id"] for h in hist]
        assert len(set(ids)) == len(ids)
        # walk the parent chain from the newest checkpoint
        step = hist[0]
        parents = 0
        while step.parent_config is not None:
            parents += 1
            step = graph.get_state(step.parent_config)
        assert parents >= 2

    def test_interrupt_and_resume(self, daemon):
        """interrupt() parks the graph with pending writes; Command(resume)
        continues from the persisted checkpoint — real HITL mechanics."""
        from langchain_core.messages import AIMessage, HumanMessage
        from langgraph.types import Command

        saver = _fresh_saver()
        graph, calls = _make_interrupt_graph(saver, script=[AIMessage(content="resumed!")])

        cfg = {"configurable": {"thread_id": "irq"}}
        graph.invoke({"messages": [HumanMessage(content="hello")]}, config=cfg)
        state = graph.get_state(cfg)
        assert state.next == ("review",) or state.next == ("chat",)
        assert state.next  # graph is parked mid-flow

        # resume: no input, same thread — continues from persisted state
        out2 = graph.invoke(Command(resume="ok"), config=cfg)
        contents = [m.content for m in out2["messages"]]
        assert any(c.startswith("R[") for c in contents)
        assert contents[-1] == "resumed!"
        assert calls == ["hello"]  # fetch was NOT re-executed after resume

    def test_time_travel_fork_tool_cached_once(self, daemon):
        """Rewind to the post-fetch checkpoint and re-run: cached_tool is
        served from ToolRecall's cache, not re-executed."""
        from langchain_core.messages import AIMessage, HumanMessage

        saver = _fresh_saver()
        graph, calls = _make_fetch_graph(saver, script=[AIMessage(content="done")])

        cfg = {"configurable": {"thread_id": "main"}}
        graph.invoke({"messages": [HumanMessage(content="hello")]}, config=cfg)
        assert calls == ["hello"]

        hist = list(graph.get_state_history(cfg))
        post_fetch = hist[-2]  # oldest is __start__; -2 is after fetch super-step
        target_id = post_fetch.config["configurable"]["checkpoint_id"]
        assert [m.content for m in post_fetch.values["messages"]][-1].startswith("F[")

        # same-thread rewind: restore that exact node, re-run the tail
        rewound = graph.get_state({"configurable": {"thread_id": "main", "checkpoint_id": target_id}})
        assert rewound.values["messages"][-1].content.startswith("F[")

        out2 = graph.invoke(None, config={"configurable": {"thread_id": "main", "checkpoint_id": target_id}})
        assert calls == ["hello"]  # tool did NOT re-run — cache served the fork
        assert [m.content for m in out2["messages"]][-1] == "done"

    def test_persistence_across_saver_instances(self, daemon):
        """Fresh saver instance (simulated process restart) reads the same
        persisted thread state from the daemon's DB."""
        from langchain_core.messages import AIMessage, HumanMessage

        saver = _fresh_saver()
        graph, _ = _make_fetch_graph(saver, script=[AIMessage(content="done")])
        cfg = {"configurable": {"thread_id": "persist"}}
        graph.invoke({"messages": [HumanMessage(content="hello")]}, config=cfg)

        saver2 = _fresh_saver()
        g2, _ = _make_fetch_graph(saver2, script=[AIMessage(content="done")])
        state = g2.get_state(cfg)
        assert [m.content for m in state.values["messages"]][-1] == "done"

    def test_subgraph_namespace(self, daemon):
        """A compiled subgraph as a node writes checkpoints under its own
        checkpoint_ns; parent composes child output; fresh saver can still
        read the parent thread state."""
        from langchain_core.messages import AIMessage, HumanMessage
        from langgraph.graph import END, START, MessagesState, StateGraph

        saver = _fresh_saver()

        def echo(state):
            return {"messages": [AIMessage(content="child-done")]}

        child = StateGraph(MessagesState)
        child.add_node("echo", echo)
        child.add_edge(START, "echo")
        child.add_edge("echo", END)
        child_compiled = child.compile(checkpointer=saver)

        parent = StateGraph(MessagesState)
        parent.add_node("sub", child_compiled)
        parent.add_edge(START, "sub")
        parent.add_edge("sub", END)
        parent_compiled = parent.compile(checkpointer=saver)

        cfg = {"configurable": {"thread_id": "sub-main"}}
        out = parent_compiled.invoke({"messages": [HumanMessage(content="ping")]}, config=cfg)
        assert [m.content for m in out["messages"]][-1] == "child-done"

        # child checkpoints exist under a non-empty namespace
        nss = set()
        for t in saver.list(cfg):
            nss.add(t.config["configurable"].get("checkpoint_ns", ""))
        assert "" in nss  # parent-level checkpoints
        assert any(ns != "" for ns in nss)  # subgraph namespace rows

        # fresh saver reads the parent thread state
        saver2 = _fresh_saver()
        parent2 = StateGraph(MessagesState)
        parent2.add_node("sub", child_compiled)
        parent2.add_edge(START, "sub")
        parent2.add_edge("sub", END)
        state = parent2.compile(checkpointer=saver2).get_state(cfg)
        assert [m.content for m in state.values["messages"]][-1] == "child-done"
