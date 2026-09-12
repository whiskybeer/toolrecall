"""verify_odysseus.py — live verification of the Odysseus adapter against an
isolated ToolRecall daemon. No LLM, no API keys, fully offline, seconds.

What it proves (all against a REAL spawned daemon, not mocks):
  1. @cached_tool sync decorator: miss → store → hit (live exec count == 1)
  2. Async decorated tool: same contract
  3. Cross-restart persistence: keys survive a daemon restart (same DB) —
     the Layer-0 byte-stability property, exercised through the adapter
  4. Rows land in the isolated DB namespaced under 'odysseus'
  5. (unit-covered elsewhere) graceful bypass when no daemon is running

Note on scope: install_agent_cache()/install_mcp_cache() are doc-stubs in the
adapter (log + return). The working integration surfaces are the decorators
(this script) and the MCP-bridge path (docs/odysseus.md).

Usage:
    .venv/bin/python bench/verify_odysseus.py
Writes bench/verify-odysseus-last.json. Exit 0 = all checks passed.
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time

BENCH_DIR = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(BENCH_DIR)

CALLS = {"sync": 0, "async": 0}


def _wait_for_socket(sock_path: str, timeout: float = 10.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if os.path.exists(sock_path):
            return True
        time.sleep(0.1)
    return False


def _spawn_daemon(workdir: str, db_path: str) -> subprocess.Popen:
    """Spawn an isolated daemon and wire THIS process's client to it.

    Same pattern as bench/gate.py: toolrecall resolves the socket and DB
    from os.environ at import time, so we set env BEFORE (re-)importing.
    """
    sock_path = os.path.join(workdir, "sock")
    env = dict(
        os.environ,
        TOOLRECALL_TRANSPORT=sock_path,
        TOOLRECALL_CACHE_DB=db_path,
        TOOLRECALL_ALLOW_TERMINAL="0",
        PYTHONPATH=REPO,
    )
    proc = subprocess.Popen(
        [sys.executable, "-m", "toolrecall", "daemon"],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    if not _wait_for_socket(sock_path):
        proc.terminate()
        raise RuntimeError("verify daemon did not create its socket in time")
    os.environ["TOOLRECALL_TRANSPORT"] = sock_path
    os.environ["TOOLRECALL_CACHE_DB"] = db_path
    for mod in [m for m in list(sys.modules) if m.startswith("toolrecall")]:
        del sys.modules[mod]
    from toolrecall.transport import DEFAULT_PATH  # noqa: E402

    assert DEFAULT_PATH == sock_path, (
        f"client wired to {DEFAULT_PATH!r}, not isolated {sock_path!r} — "
        "would measure the production daemon"
    )
    return proc


def _stop_daemon(proc: subprocess.Popen) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


def _odysseus_rows(db_path: str) -> int:
    """Count rows namespaced under 'odysseus' across candidate tables."""
    if not os.path.exists(db_path):
        return 0
    con = sqlite3.connect(db_path)
    try:
        tables = [
            r[0]
            for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        ]
        n = 0
        for t in tables:
            cols = {c[1] for c in con.execute(f'PRAGMA table_info("{t}")')}
            if "mcp_server" in cols:
                n += con.execute(
                    f"SELECT COUNT(*) FROM \"{t}\" WHERE mcp_server='odysseus'"
                ).fetchone()[0]
        return n
    finally:
        con.close()


def _make_tools():
    """Fresh decorated tools bound to the CURRENTLY imported toolrecall."""
    from toolrecall.adapters.odysseus import cached_async_tool, cached_tool

    @cached_tool(ttl=300)
    def odysseus_search_web(query: str, limit: int = 5) -> dict:
        CALLS["sync"] += 1
        return {"query": query, "limit": limit, "results": ["r1", "r2"]}

    @cached_async_tool(ttl=300)
    async def odysseus_fetch_data(url: str) -> dict:
        CALLS["async"] += 1
        return {"url": url, "status": 200, "body": "x" * 100}

    return odysseus_search_web, odysseus_fetch_data


def run_phase_live() -> dict:
    """Phase 1: daemon up — miss, hit, distinct-args miss, async contract."""
    odysseus_search_web, odysseus_fetch_data = _make_tools()

    # family convention: plain cached_tool auto-routes async fns too
    from toolrecall.adapters.odysseus import cached_tool

    @cached_tool
    async def _auto_routed(url: str) -> dict:
        CALLS["async"] += 1
        return {"url": url, "status": 200, "body": "y" * 100}

    z1 = asyncio.run(_auto_routed("https://example.test/z"))
    z2 = asyncio.run(_auto_routed("https://example.test/z"))
    assert z1 == z2 and CALLS["async"] == 1, (z1, z2, CALLS)

    r1 = odysseus_search_web("héllo wörld", limit=3)  # miss
    r2 = odysseus_search_web("héllo wörld", limit=3)  # hit
    assert r1 == r2, "cached result must equal live result"
    assert CALLS["sync"] == 1, f"sync live execs after miss+hit: {CALLS}"

    odysseus_search_web("other query")  # distinct args → miss
    assert CALLS["sync"] == 2, CALLS
    odysseus_search_web("héllo wörld", limit=3)  # hit again
    assert CALLS["sync"] == 2, CALLS

    a1 = asyncio.run(odysseus_fetch_data("https://example.test/a"))
    a2 = asyncio.run(odysseus_fetch_data("https://example.test/a"))
    assert a1 == a2
    # 2 live execs total: 1 auto-routed pair (1 exec) + 1 fetch_data exec
    assert CALLS["async"] == 2, CALLS

    return {
        "sync_miss_hit": "PASS (live execs: 2, hits: 2, distinct-args miss: 1)",
        "async_decorated": "PASS (live execs: 1, hits: 1)",
    }


def run_phase_restart() -> dict:
    """Phase 2: same decorated call after a daemon restart on the SAME db.
    Byte-stable keys ⇒ hit with zero live execution."""
    odysseus_search_web, _fetch = _make_tools()
    CALLS["sync"] = 0
    r = odysseus_search_web("héllo wörld", limit=3)
    assert CALLS["sync"] == 0, (
        f"post-restart call re-executed ({CALLS}) — key not stable across restart"
    )
    assert r["query"] == "héllo wörld"
    return {"cross_restart_hits": "PASS (0 live execs after daemon restart)"}


def main() -> int:
    workdir = tempfile.mkdtemp(prefix="tr_verify_odysseus_")
    db_path = os.path.join(workdir, "cache.db")
    report: dict = {"phases": {}}
    try:
        proc = _spawn_daemon(workdir, db_path)
        try:
            report["phases"]["live_daemon"] = run_phase_live()
        finally:
            _stop_daemon(proc)

        proc2 = _spawn_daemon(workdir, db_path)  # restart on the SAME db
        try:
            report["phases"]["restart"] = run_phase_restart()
        finally:
            _stop_daemon(proc2)

        rows = _odysseus_rows(db_path)
        report["phases"]["db_namespace"] = "PASS" if rows >= 2 else f"FAIL (odysseus rows: {rows})"
    except Exception as e:  # noqa: BLE001
        report["error"] = repr(e)
        print(json.dumps(report, indent=2))
        return 1

    ok = all("PASS" in str(v) for v in report["phases"].values())
    report["result"] = "PASS" if ok else "FAIL"
    with open(os.path.join(BENCH_DIR, "verify-odysseus-last.json"), "w") as f:
        json.dump(report, f, indent=2)
    print(json.dumps(report, indent=2))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
