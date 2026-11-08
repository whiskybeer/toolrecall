"""Standalone runner: real-langgraph e2e scenarios for the LangGraph adapter.

Adapter reality (verified 2026-09-10): toolrecall adapters call the cache
IN-PROCESS via toolrecall.cache (direct SQLite through toolrecall._db) — they
never talk to the daemon socket. So this runner does NOT spawn a daemon; it
points TOOLRECALL_CACHE_DB at a hermetic temp file BEFORE any toolrecall
import and mocks client.daemon_running=True (the graceful-bypass gate), then
runs real LangGraph 1.x graphs against the real adapter code path.

Scenarios: multi-node message-state graph + history/parents, functional
interrupt() + Command(resume), time-travel rewind with cached_tool served
from cache, persistence across saver instances, subgraph checkpoint_ns.

Why standalone instead of pytest: pytest hangs at interpreter shutdown when
langgraph's executor threads are present (idle worker pools; see skill
toolrecall-adapter-authoring). This runner uses per-scenario watchdogs and a
hard os._exit to skip the shutdown join.

Run: PYTHONPATH=$(git rev-parse --show-toplevel) /tmp/lgvenv/bin/python tests/run_langgraph_e2e_standalone.py
"""

import faulthandler
import itertools
import os
import signal
import sys
import tempfile
import threading

# ---- hermetic env BEFORE any toolrecall import -------------------------------
_TMP = tempfile.mkdtemp(prefix="lg_e2e_")
os.environ["TOOLRECALL_CACHE_DB"] = os.path.join(_TMP, "cache.db")
os.environ["TOOLRECALL_KNOWLEDGE_DB"] = os.path.join(_TMP, "knowledge.db")
os.environ["TOOLRECALL_TRANSPORT"] = os.path.join(_TMP, "no-daemon.sock")
os.environ["TOOLRECALL_NO_AUTOSTART"] = "1"
os.environ["TOOLRECALL_UPDATE_CHECK"] = "0"

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Bootstrap the hermetic DB schema (fresh process, fresh file).
from toolrecall._db import _init  # noqa: E402
from toolrecall.cache import SCHEMA  # noqa: E402

_init(SCHEMA)

import toolrecall.client as tr_client  # noqa: E402

tr_client.daemon_running = lambda: True  # in-process cache path; no socket

TIMEOUT_S = 60


def _scripted_model(script):
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel

    # LAZY cycle — never list() an infinite iterator (that was the e2e hang).
    return GenericFakeChatModel(messages=itertools.cycle(list(script)))


def _fresh_saver(ttl=600):
    from toolrecall.adapters.langgraph import get_saver

    return get_saver(ttl=ttl)


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
        out = e2e_fetch(state["messages"][-1].content + f"#{id(saver) % 100000}")
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
        out = e2e_fetch(state["messages"][-1].content + f"#{id(saver) % 100000}")
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


# ---- scenarios ----------------------------------------------------------------


def sc_multi_node():
    from langchain_core.messages import AIMessage, HumanMessage

    saver = _fresh_saver()
    graph, calls = _make_fetch_graph(saver, script=[AIMessage(content="done")])
    cfg = {"configurable": {"thread_id": "main"}}
    print("  invoke...", flush=True)
    out = graph.invoke({"messages": [HumanMessage(content="hello")]}, config=cfg)
    assert [m.content for m in out["messages"]][-1] == "done"
    assert len(calls) == 1
    print("  get_state_history...", flush=True)
    hist = list(graph.get_state_history(cfg))
    ids = [h.config["configurable"]["checkpoint_id"] for h in hist]
    print(f"  history={len(hist)} ids={[i[:13] for i in ids]}", flush=True)
    assert len(hist) >= 3, f"history too short: {len(hist)}"
    assert len(set(ids)) == len(ids), f"duplicate checkpoint ids: {ids}"
    print(f"  hist[0].parent_config={hist[0].parent_config}", flush=True)
    # adapter-side ground truth: what parents did we actually store?
    core = saver._core
    for cid in core.list_ids("main", ""):
        row = core.get("main", "", cid)
        p = row["parent"]["id"] if row and row.get("parent") else None
        print(f"  stored {cid[:13]} parent={p[:13] if p else None}", flush=True)
    print(f"  history={len(hist)}; walking parents...", flush=True)
    step, parents = hist[0], 0
    while step.parent_config is not None:
        parents += 1
        nxt = graph.get_state(step.parent_config)
        print(
            f"  hop {parents}: -> {nxt.config['configurable']['checkpoint_id'][:8] if nxt else None}, parent={'yes' if nxt.parent_config else 'no'}",
            flush=True,
        )
        if (
            nxt is None
            or nxt.config["configurable"]["checkpoint_id"]
            == step.config["configurable"]["checkpoint_id"]
        ):
            break
        step = nxt
    print(f"  walked {parents} hops", flush=True)
    assert parents >= 2, f"parent chain too short: walked {parents} hops"
    print(f"  OK: history={len(hist)}, parent-chain={parents}, tool_calls={calls}", flush=True)


def sc_interrupt_resume():
    from langchain_core.messages import AIMessage, HumanMessage
    from langgraph.types import Command

    saver = _fresh_saver()
    graph, calls = _make_interrupt_graph(saver, script=[AIMessage(content="resumed!")])
    cfg = {"configurable": {"thread_id": "irq"}}
    print("  invoke (park)...", flush=True)
    graph.invoke({"messages": [HumanMessage(content="hello")]}, config=cfg)
    state = graph.get_state(cfg)
    assert state.next, "graph must park mid-flow"
    print(f"  parked at next={state.next}; resume...", flush=True)
    out2 = graph.invoke(Command(resume="ok"), config=cfg)
    contents = [m.content for m in out2["messages"]]
    assert any(c.startswith("R[") for c in contents), contents
    assert contents[-1] == "resumed!"
    assert len(calls) == 1, (
        f"fetch re-ran after resume: {calls}"
    )  # resume must not re-execute pre-interrupt work
    print(f"  OK: resumed, tail={contents[-2:]}, tool_calls={calls}", flush=True)


def sc_time_travel():
    import uuid

    from langchain_core.messages import AIMessage, HumanMessage

    saver = _fresh_saver()
    graph, calls = _make_fetch_graph(saver, script=[AIMessage(content="done")])
    cfg = {"configurable": {"thread_id": f"tt-{uuid.uuid4().hex[:8]}"}}
    print("  first run...", flush=True)
    graph.invoke({"messages": [HumanMessage(content="hello")]}, config=cfg)
    assert len(calls) == 1, f"first run tool calls: {calls}"

    hist = list(graph.get_state_history(cfg))
    # find the post-fetch checkpoint by CONTENT (history order varies by
    # langgraph version): the newest snapshot whose tail message starts F[
    post_fetch = None
    for h in hist:
        tail = [m.content for m in h.values["messages"]][-1]
        if tail.startswith("F["):
            post_fetch = h
            break
    assert post_fetch is not None, (
        f"no post-fetch checkpoint in {[[m.content for m in h.values['messages']][-1] for h in hist]}"
    )
    target_id = post_fetch.config["configurable"]["checkpoint_id"]
    print(f"  rewind to {target_id[:8]}...", flush=True)
    rewound = graph.get_state(
        {
            "configurable": {
                "thread_id": cfg["configurable"]["thread_id"],
                "checkpoint_id": target_id,
            }
        }
    )
    rw_tail = [m.content for m in rewound.values["messages"]][-1]
    assert rw_tail.startswith("F["), f"rewound tail: {rw_tail}"

    print("  replay from rewind...", flush=True)
    out2 = graph.invoke(
        None,
        config={
            "configurable": {
                "thread_id": cfg["configurable"]["thread_id"],
                "checkpoint_id": target_id,
            }
        },
    )
    assert len(calls) == 1, f"tool re-ran on rewind: {calls}"
    out_tail = [m.content for m in out2["messages"]][-1]
    assert out_tail == "done", f"replay tail: {out_tail}"
    print(f"  OK: tool_calls={calls} (cache served the fork)", flush=True)


def sc_persistence():
    from langchain_core.messages import AIMessage, HumanMessage

    saver = _fresh_saver()
    graph, _ = _make_fetch_graph(saver, script=[AIMessage(content="done")])
    cfg = {"configurable": {"thread_id": "persist"}}
    print("  first run...", flush=True)
    graph.invoke({"messages": [HumanMessage(content="hello")]}, config=cfg)

    print("  fresh saver instance reads state...", flush=True)
    saver2 = _fresh_saver()
    g2, _ = _make_fetch_graph(saver2, script=[AIMessage(content="done")])
    state = g2.get_state(cfg)
    assert [m.content for m in state.values["messages"]][-1] == "done"
    print("  OK: persisted across saver instances", flush=True)


def sc_subgraph():
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
    print("  parent invoke with subgraph node...", flush=True)
    out = parent_compiled.invoke({"messages": [HumanMessage(content="ping")]}, config=cfg)
    assert [m.content for m in out["messages"]][-1] == "child-done"

    nss = {t.config["configurable"].get("checkpoint_ns", "") for t in saver.list(cfg)}
    # Listing is per-namespace (same as SqliteSaver). Subgraph states for a
    # COMPLETED run aren't in get_state(subgraphs=True) (no pending subgraph
    # task); discover the child namespace from history snapshots' tasks, then
    # read the child state through the parent (ns routing).
    assert "" in nss, nss
    child_ns = None
    for snap in parent_compiled.get_state_history(cfg):
        for task in snap.tasks:
            tstate = getattr(task, "state", None)
            tns = (
                (tstate or {}).get("configurable", {}).get("checkpoint_ns")
                if isinstance(tstate, dict)
                else None
            )
            if tns:
                child_ns = tns
                break
        if child_ns:
            break
    print(f"  parent ns rows={len(nss)}, child_ns={child_ns}", flush=True)
    assert child_ns, "no subgraph namespace found in history tasks"
    child_snap = parent_compiled.get_state(
        {"configurable": {"thread_id": cfg["configurable"]["thread_id"], "checkpoint_ns": child_ns}}
    )
    child_tail = [m.content for m in child_snap.values["messages"]][-1]
    assert child_tail == "child-done", f"child state tail: {child_tail}"
    print(f"  OK: child state via ns routing: {child_tail}", flush=True)


SCENARIOS = [
    ("multi-node run + history + parent chain", sc_multi_node),
    ("interrupt() + Command(resume)", sc_interrupt_resume),
    ("time-travel rewind, tool cached once", sc_time_travel),
    ("persistence across saver instances", sc_persistence),
    ("subgraph checkpoint_ns", sc_subgraph),
]


def main():
    faulthandler.register(signal.SIGALRM)  # dump all stacks if the watchdog fires

    results = []
    for name, fn in SCENARIOS:
        print(f"\n== {name}", flush=True)
        watchdog = threading.Timer(TIMEOUT_S, lambda: os.kill(os.getpid(), signal.SIGALRM))
        watchdog.start()
        try:
            fn()
            results.append((name, True, ""))
            print("  -> PASS", flush=True)
        except BaseException as e:  # noqa: BLE001 — report, don't crash
            results.append((name, False, repr(e)))
            print(f"  -> FAIL: {e!r}", flush=True)
        finally:
            watchdog.cancel()

    print("\n==== RESULTS ====")
    failed = 0
    for name, ok, err in results:
        print(f" {'PASS' if ok else 'FAIL'}  {name}" + (f" — {err}" if not ok else ""))
        failed += 0 if ok else 1
    print(f"\n{len(results) - failed}/{len(results)} scenarios passed", flush=True)
    sys.stdout.flush()
    os_ = __import__("os")
    os_._exit(1 if failed else 0)  # hard exit: skips interpreter shutdown thread-join hang


if __name__ == "__main__":
    main()
