"""Transport arming: the flow becomes agent-driven without agent etiquette.

``armed`` used to be reachable only by an agent voluntarily calling
``context_set_checkpoint`` — the convention in docs/AGENTS.md that no shipped
harness follows (the benchmark harness does, which is why only benchmarks
reported the endurance effect). The daemon now arms on the first request that
carries an ``agent_id``: no extra round trip, no agent cooperation, no
harness-specific hook.
"""

from toolrecall.context_tracker import ContextTracker
from toolrecall.daemon import DaemonServer


def _server() -> DaemonServer:
    """A DaemonServer with a real tracker and no socket — enough to test _route."""
    server = DaemonServer.__new__(DaemonServer)
    server._context = ContextTracker(ignore_prefixes=())
    return server


def test_arm_sets_flag_and_reports_first_transition():
    tracker = ContextTracker(ignore_prefixes=())
    first = tracker.arm(source="mcp_bridge")
    assert first == {"armed": True, "already": False, "armed_by": "mcp_bridge"}

    second = tracker.arm(source="proxy")
    assert second["already"] is True
    assert tracker.is_armed() is True


def test_arm_does_not_bank_tokens_or_move_the_checkpoint(tmp_path):
    tracker = ContextTracker(ignore_prefixes=())
    read_file = tmp_path / "module.py"
    read_file.write_text("x = 1\n" * 1000)
    tracker.mark_read(str(read_file))
    before = tracker.get_stats()

    tracker.arm(source="mcp_bridge")

    after = tracker.get_stats()
    assert after["agent_armed"] is True
    # Arming is pure state: no banking, no checkpoint advance, no drop counter.
    assert after["ctx_dropped_tokens_total"] == before["ctx_dropped_tokens_total"]
    assert after["checkpoint"] == before["checkpoint"]
    assert after["total_clean"] == before["total_clean"]


def test_route_arms_on_a_request_that_carries_an_agent_id():
    server = _server()
    assert server._context.is_armed() is False

    server._route({"cmd": "ping", "agent_id": "any-harness"})

    assert server._context.is_armed() is True


def test_route_does_not_arm_without_identity():
    server = _server()
    server._route({"cmd": "ping"})
    assert server._context.is_armed() is False
