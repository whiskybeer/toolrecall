"""Regression tests for the MCP subprocess leak guard.

Context: a single pytest run against toolrecall started MCP servers lazily and
never reaped them, leaving 84 ``uvx mcp-server-sqlite`` processes (168 counting
their uv wrappers) alive for over an hour. On a 4 GB host that exhausted memory,
starved the page cache, and made the *unrelated* Hermes WebUI look broken
(7-24 s API responses). These tests pin the three defences:

1. a started session is tracked in the process-wide registry,
2. ``shutdown_all_mcp_sessions()`` reaps it (what conftest teardown calls),
3. garbage-collecting a session reaps it too (``__del__`` guard).
"""

import gc
import os
import signal
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from toolrecall import daemon as daemon_mod
from toolrecall.daemon import MCPClientSession, shutdown_all_mcp_sessions


def _sleeper_command():
    """A stand-in server: a real child process that outlives the test if leaked."""
    return sys.executable, ["-c", "import time; time.sleep(120)"]


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _make_session():
    cmd, args = _sleeper_command()
    session = MCPClientSession(name="leak-test", command=cmd, args=args)
    session._start()
    assert session.running, "session should have started a child process"
    return session


def test_started_session_is_tracked_and_reaped_by_registry():
    session = _make_session()
    proc = session._proc
    pid = proc.pid
    try:
        assert session in daemon_mod._LIVE_MCP_SESSIONS
        stopped = shutdown_all_mcp_sessions()
        assert stopped >= 1
        proc.wait(timeout=5)
        assert not _alive(pid), "registry shutdown must reap the child"
    finally:
        try:
            session.shutdown()
        except Exception:
            pass


def test_shutdown_deregisters_session():
    session = _make_session()
    pid = session._proc.pid
    try:
        session.shutdown()
        assert session not in daemon_mod._LIVE_MCP_SESSIONS
        assert not _alive(pid)
    finally:
        try:
            session.shutdown()
        except Exception:
            pass


def test_gc_of_abandoned_session_reaps_child():
    session = _make_session()
    pid = session._proc.pid
    # Drop every strong reference and let the cycle collector run.
    del session
    gc.collect()
    deadline = time.time() + 5
    while time.time() < deadline and _alive(pid):
        time.sleep(0.1)
    assert not _alive(pid), "__del__ guard must reap an abandoned session's child"


def test_no_sessions_left_after_suite_teardown_helper():
    # The conftest autouse fixture calls this after every test; a clean call
    # must be harmless and report zero when nothing is running.
    shutdown_all_mcp_sessions()
    assert len(daemon_mod._LIVE_MCP_SESSIONS) == 0
