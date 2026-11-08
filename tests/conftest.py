"""Shared test fixtures — hermeticity for the ToolRecall test suite.

Two layers of protection:

1. **Env isolation (autouse fixture)** — every test runs with
   ``TOOLRECALL_CACHE_DB`` / ``TOOLRECALL_KNOWLEDGE_DB`` pointed into a
   per-test temp dir, so any code path that reads config and opens the cache
   DB lands in scratch space, not ``~/.toolrecall/``.

2. **Production-path guard (autouse fixture)** — fails the test (not silently
   skips) if the test or code under test touches real production state:
   the live daemon socket, the live DB files, port 8569 (forward proxy), or
   **writes a file inside the live state home or the live config home**. The
   write arm was added 2026-09-26: the guard watched sqlite3.connect and
   socket.connect only, so
   two HTTP harnesses appended fake rows to the billed-truth usage log
   (~/.toolrecall/proxy_usage.csv) for weeks without a single failure —
   2048 rows across api.deduptest.invalid and api.testretry.invalid. Live log
   appends and the update-hint cache are carved out via `_PROD_WRITE_EXEMPT`.
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
# Log-rail tag: every daemon/test log line this pytest process emits into a
# shared log file carries [hermetic] so production-log readers can tell the
# two apart at a glance (test-suite "tracker snapshot saved: 0 read paths"
# lines were twice misread as a dead production flow — 2026-09-15).
os.environ["TOOLRECALL_HERMETIC"] = "1"
# Hermetic DBs at collection time: avoid importing cache.py against live ~/.toolrecall
os.environ.setdefault(
    "TOOLRECALL_CACHE_DB",
    os.path.join(tempfile.gettempdir(), "toolrecall-hermetic-import.db"),
)
os.environ.setdefault(
    "TOOLRECALL_KNOWLEDGE_DB",
    os.path.join(tempfile.gettempdir(), "toolrecall-hermetic-import-knowledge.db"),
)


def _points_at_prod_state(val: str) -> bool:
    """True if *val* resolves inside the live ToolRecall state home.

    Any path under ~/.toolrecall is live state (cache.db, -wal/-shm,
    knowledge.db, daemon.pid, logs) — a test session pinned there is
    #1-way-to-corrupt-production territory. Callers pass an already-set
    env value; an UNSET var never refuses (the autouse fixture pins a
    temp path per-test instead).
    """
    expanded = os.path.expanduser(os.path.expandvars(val))
    prod_home = os.path.realpath(os.path.expanduser("~/.toolrecall"))
    candidate = os.path.realpath(expanded)
    return candidate == prod_home or candidate.startswith(prod_home + os.sep)


# Layer 0 — session-wide refusal (module scope, runs BEFORE any test module
# imports toolrecall): if the operator/environment pinned a production-looking
# cache/knowledge DB explicitly, the session is misconfigured — abort the
# ENTIRE run before any DB is opened. Mirrors benchmark_workload.py's REFUSING
# guard for the pytest path. Why session-level: the runtime prod-path guard
# below only reports at TEARDOWN (violations are collected, fail() fires at
# the end) — by then a misconfigured wipe already happened (the 2026-09-10
# wipe storm: 17 production reset_stats() calls from test/bench processes).
# `os.environ.setdefault("TOOLRECALL_CACHE_DB", ...)` in test modules loses
# exactly this race when a prior import already resolved config — this guard
# makes the loser abort loudly instead of silently hitting live state.
for _var in ("TOOLRECALL_CACHE_DB", "TOOLRECALL_KNOWLEDGE_DB", "BENCH_DB_PATH"):
    _val = os.environ.get(_var)
    if _val and _points_at_prod_state(_val):
        pytest.exit(
            f"REFUSING to run: {_var}={_val} points at production ToolRecall "
            f"state (~/.toolrecall). Pin it to a temp/scratch path — the "
            f"autouse fixtures do that automatically when the var is unset. "
            f"(benchmark_workload.py enforces the same rule outside pytest.)",
            returncode=3,
        )

# Production identifiers — a test touching these without the exempt marker
# is testing against live state (the #1 way to silently corrupt it).
_PROD_PATTERNS = [
    "/run/user/",  # live daemon socket dir
    "toolrecall.sock",
    os.path.expanduser("~/.toolrecall"),  # live DB / state home: usage log, logs, recall DB
    # Live config home — a SECOND directory, and the guard's blind spot for a
    # long time: a test writing toolrecall.toml/.env here corrupts the user's
    # real settings while the guard stays silent (same failure shape as the
    # usage-log leak: the write arm simply did not look here).
    os.path.expanduser("~/.config/toolrecall"),
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

# Paths under the state home whose WRITES are legitimate test activity — exempt
# from the write arm only (reads and connects to them stay guarded):
#   - ``logs/`` — the logging rail is default-ON, so merely exercising a logged
#     code path appends to the live log. Append-only diagnostics; no quoted
#     number is derived from them.
#   - ``last_update_check.json`` — atomic (``.tmp`` + rename) update-hint cache
#     rewritten by the daemon update-hint tests; self-healing, not billed truth.
# The write arm exists for the OPPOSITE case: a plain ``open(path, "a")``
# appending rows to the billed-truth usage log (~/.toolrecall/proxy_usage.csv),
# which two HTTP harnesses did for weeks whenever they ran.
_PROD_WRITE_EXEMPT = ("/logs/", "last_update_check.json")


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
    """Point TOOLRECALL_* path env vars at a per-test temp DB — unconditionally.

    Why force-pin (no "only set if unset" leniency): pytest IMPORTS every
    test module at collection time, and several modules set a raw
    ``os.environ["TOOLRECALL_CACHE_DB"] = <fixed path>`` at module level
    (e.g. test_coalescing_e2e.py, test_file_ttls.py, test_invalidate_scope.py,
    test_ast_security.py, test_audit_fixes.py). That raw value survives
    collection and is never undone, so the previous lenient pin ("we only
    set if unset") silently left EVERY test without its own fixture on ONE
    shared DB. Combined with cache keys that collide across tests (adapter
    tools named ``tool`` with identical args, saver thread ids like ``t1``),
    earlier tests' rows leak into later tests' lookups — order-dependent
    failures that never reproduce when files run alone.

    Tests that deliberately pin their own DB do so inside fixtures with
    ``monkeypatch.setenv`` — those run AFTER this autouse fixture and still
    win. The session-level Layer-0 guard above already aborts the whole run
    when the operator pins a production path, so force-pinning here cannot
    mask a misconfigured session.

    Also disables the docs auto-refresh (TOOLRECALL_DOCS_INDEX_TTL=0):
    a background refresh thread spawned by docs_search() resolves the DB
    path at *thread execution time* — after the test's own env was torn
    down — racing the next test's connections (rotating 'database is
    locked' / 'no such table' flakes in test_memory_index). Tests that
    exercise the refresh explicitly set their own TTL.
    """
    for var in ("TOOLRECALL_CACHE_DB", "TOOLRECALL_KNOWLEDGE_DB", "BENCH_DB_PATH"):
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
    """Fail any test that connects to the live daemon socket / live DB / :8569,
    or writes a file inside the live state home.

    Implemented as a sqlite3 connect + socket connect + open-for-write audit
    rather than patching every entry point: we wrap the actual escape hatches
    (sqlite3.connect, socket.socket.connect, builtins.open) for the duration of
    the test. Writes are audited because the production usage log is written
    with a plain ``open(path, "a")`` — a path sqlite/socket wrapping cannot see.
    """
    if _exempt(request.node):
        yield
        return

    import builtins
    import socket as _socket
    import sqlite3 as _sqlite3

    orig_connect = _sqlite3.connect
    orig_socket_connect = _socket.socket.connect
    orig_open = builtins.open
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

    def guarded_open(file, mode="r", *a, **kw):
        """Flag WRITES into production state. Reads are not flagged: a
        diagnostic test may legitimately reference a live path, and string
        assertions never open it. A write is what pollutes production.

        ``_PROD_WRITE_EXEMPT`` carve-outs cover state that code-under-test
        legitimately rewrites (live log appends, the update-hint cache).
        """
        try:
            m = kw.get("mode", mode)
            if isinstance(m, str) and any(ch in m for ch in "wax+"):
                p = str(file)
                if _is_prod(p) and not any(x in p for x in _PROD_WRITE_EXEMPT):
                    violations.append(f"open({p!r}, mode={m!r})")
        except Exception:  # noqa: BLE001 — the audit must never alter behaviour
            pass
        return orig_open(file, mode, *a, **kw)

    _sqlite3.connect = guarded_sqlite_connect
    _socket.socket.connect = guarded_socket_connect
    builtins.open = guarded_open
    try:
        yield
    finally:
        _sqlite3.connect = orig_connect
        _socket.socket.connect = orig_socket_connect
        builtins.open = orig_open
        if violations:
            pytest.fail(
                "Test touched production state: " + "; ".join(sorted(violations)),
                pytrace=False,
            )


@pytest.fixture(autouse=True)
def _reap_mcp_sessions():
    """Leak guard: stop any MCP server subprocess this test started.

    The daemon tracks every started MCP subprocess in
    ``toolrecall.daemon._LIVE_MCP_SESSIONS`` and reaps them at interpreter
    exit, but a long run would still accumulate one live server per test until
    the run ends. One pytest run leaked 84 ``uvx mcp-server-sqlite`` processes
    (168 with their uv wrappers) that way and starved the host of memory — the
    box then looked broken to every other service on it.
    """
    try:
        from toolrecall import daemon as _daemon
    except Exception:  # noqa: BLE001 — never fail a test on import trouble
        yield
        return
    try:
        yield
    finally:
        try:
            _daemon.shutdown_all_mcp_sessions()
        except Exception:  # noqa: BLE001
            pass
