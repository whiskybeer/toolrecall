"""Shared test fixtures — hermeticity for the ToolRecall test suite.

Two layers of protection:

1. **Env isolation (autouse fixture)** — every test runs with
   ``TOOLRECALL_CACHE_DB`` / ``TOOLRECALL_KNOWLEDGE_DB`` pointed into a
   per-test temp dir, so any code path that reads config and opens the cache
   DB lands in scratch space, not ``~/.toolrecall/``.

2. **Production-path guard (autouse fixture)** — fails the test (not silently
   skips) if the test or code under test touches real production state:
   the live daemon socket, the live DB files, or port 8569 (forward proxy).
   Exempt via ``@pytest.mark.allow_prod_paths`` — used only by tests that
   deliberately stub these (e.g. test_cli's shell-text assertions).
"""

import os
import sys

import pytest
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Hermetic transport: any in-process daemon connection must target a socket
# that can never be the live daemon. TOOLRECALL_TRANSPORT is read once at
# transport import time (module-level DEFAULT_PATH), so this MUST be set
# before any test module imports toolrecall — conftest is imported first,
# making top-level assignment here the only reliable hook. Tests needing a
# real daemon spawn their own on a temp socket (tests.e2e_helpers.E2EDaemon);
# live-daemon integration tests skip gracefully when the ping fails.
os.environ["TOOLRECALL_TRANSPORT"] = os.path.join(
    tempfile.gettempdir(), "toolrecall-hermetic-no-daemon.sock"
)
# No daemon auto-start from inside pytest: _ensure_daemon() must never spawn
# a daemon that would bind the hermetic (or worse, the live) socket.
os.environ["TOOLRECALL_NO_AUTOSTART"] = "1"

# Production identifiers — a test touching these without the exempt marker
# is testing against live state (the #1 way to silently corrupt it).
_PROD_PATTERNS = [
    "/run/user/",  # live daemon socket dir
    "toolrecall.sock",
    os.path.expanduser("~/.toolrecall"),  # live DB / config home
    "localhost:8569",
    "127.0.0.1:8569",
]

# Hermetic sockets live under the system temp dir (e.g. /tmp/toolrecall_e2e_*).
# Those are the sanctioned test-isolation location — a socket path inside
# tempfile.gettempdir() is never production state, even though it may contain
# "toolrecall.sock" as a name component.
_TEMPDIR = os.path.realpath(tempfile.gettempdir())

# Modules/tests that legitimately reference production paths (string-rewrite
# tests, transport tests that patch DEFAULT_PATH to a temp path, etc.) can
# opt out with @pytest.mark.allow_prod_paths.
_PROD_EXEMPT_FILES = {
    # test_cli asserts on shell-text rewriting of :8569 lines — pure string work.
    "test_cli.py",
    # test_client's docstrings/mocks reference the live socket path but patch
    # transport.DEFAULT_PATH before any I/O. Keep exempt until converted.
    "test_client.py",
    "test_transport.py",
    "test_healthcheck.py",
    "test_integration.py",
    "test_e2e_shim.py",
    "exempt-placeholder-none-yet",
}


def _exempt(item) -> bool:
    fname = os.path.basename(item.fspath)
    if fname in _PROD_EXEMPT_FILES:
        return True
    return bool(item.get_closest_marker("allow_prod_paths"))


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "allow_prod_paths: test deliberately references production paths/ports",
    )


def pytest_runtest_setup(item):
    # Preserve the original contract: e2e/adk tests need the real client module.
    if item.get_closest_marker("e2e") or item.get_closest_marker("adk"):
        import importlib

        import toolrecall.client

        importlib.reload(toolrecall.client)
        from toolrecall.client import daemon_running as real_func

        toolrecall.client.daemon_running = real_func


@pytest.fixture(autouse=True)
def _isolated_toolrecall_env(monkeypatch, tmp_path, request):
    """Point TOOLRECALL_* path env vars at per-test temp dirs.

    Tests that set their own TOOLRECALL_* vars still win (we only set if
    unset or already pointing at production home).

    Also disables the docs auto-refresh (TOOLRECALL_DOCS_INDEX_TTL=0):
    a background refresh thread spawned by docs_search() resolves the DB
    path at *thread execution time* — after the test's own env was torn
    down — racing the next test's connections (rotating 'database is
    locked' / 'no such table' flakes in test_memory_index). Tests that
    exercise the refresh explicitly set their own TTL.
    """
    for var in ("TOOLRECALL_CACHE_DB", "TOOLRECALL_KNOWLEDGE_DB", "BENCH_DB_PATH"):
        cur = os.environ.get(var)
        if not cur or cur.startswith(os.path.expanduser("~/.toolrecall")):
            monkeypatch.setenv(var, str(tmp_path / "hermetic.db"))
    monkeypatch.setenv("TOOLRECALL_DOCS_INDEX_TTL", "0")
    # Auto-updater: disabled by default in tests (same rationale as the docs
    # refresh — a check_for_update() fired from main() under test would hit
    # the real PyPI API and write real ~/.toolrecall state). Dedicated
    # updater tests opt in via their own env patches.
    monkeypatch.setenv("TOOLRECALL_UPDATE_CHECK", "0")
    yield


@pytest.fixture(autouse=True)
def _production_path_guard(request):
    """Fail any test that connects to the live daemon socket / live DB / :8569.

    Implemented as a sqlite3 connect + socket connect audit rather than
    patching every entry point: we wrap the actual escape hatches
    (sqlite3.connect, socket.socket.connect) for the duration of the test.
    """
    if _exempt(request.node):
        yield
        return

    import socket as _socket
    import sqlite3 as _sqlite3

    orig_connect = _sqlite3.connect
    orig_socket_connect = _socket.socket.connect
    violations = []

    def _is_prod(path_str: str) -> bool:
        if path_str.startswith(_TEMPDIR + os.sep) or path_str == _TEMPDIR:
            return False
        return any(pat in path_str for pat in _PROD_PATTERNS)

    def guarded_sqlite_connect(path, *a, **kw):
        p = str(path)
        if _is_prod(p):
            violations.append(f"sqlite3.connect({p!r})")
        return orig_connect(path, *a, **kw)

    def guarded_socket_connect(self, address):
        s = str(address)
        if _is_prod(s):
            violations.append(f"socket.connect({s!r})")
        return orig_socket_connect(self, address)

    _sqlite3.connect = guarded_sqlite_connect
    _socket.socket.connect = guarded_socket_connect
    try:
        yield
    finally:
        _sqlite3.connect = orig_connect
        _socket.socket.connect = orig_socket_connect
        if violations:
            pytest.fail(
                "Test touched production state: " + "; ".join(sorted(violations)),
                pytrace=False,
            )
