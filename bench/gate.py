"""gate.py — CI benchmark regression gate (constitution A7).

Fails when ToolRecall's file-cache hit behavior regresses. Drives the REAL
cache layer (toolrecall.client.cached_read → daemon → SQLite) against a
fixed fixture set in an isolated environment — no LLM, no API keys, fully
deterministic, runs in seconds.

Why not `run_arm.py --dry-run`?  The dry-run dummy agent skips the tool
layer entirely (no file reads → no cache activity), so it cannot measure
hit rates. The gate instead exercises the exact production path with a
spawned daemon and an isolated TOOLRECALL_CACHE_DB.

Phases (all on the same isolated daemon + DB):
  cold          — first read of each fixture → MUST miss (cached=False)
  warm          — re-read the same fixtures N turns → MUST hit every time
  invalidation  — rewrite one fixture (external write, mtime change) →
                  next read MUST miss, the read after MUST hit again

Metrics written to bench/gate-last.json and compared against
bench/baseline.json (committed). Exit 1 on regression.

Usage:
    python3 bench/gate.py                    # gate against baseline
    python3 bench/gate.py --update-baseline  # rewrite baseline (justify in PR!)
    python3 bench/gate.py --turns 10         # smaller warm phase (tests)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time

BENCH_DIR = os.path.dirname(os.path.abspath(__file__))
FIXTURE_SRC = os.path.join(BENCH_DIR, "data", "large-review")
BASELINE_PATH = os.path.join(BENCH_DIR, "baseline.json")
LAST_RUN_PATH = os.path.join(BENCH_DIR, "gate-last.json")

FIXTURES = ["large-file-1.txt", "large-file-2.txt", "large-file-3.txt"]
WARM_TURNS = 25  # 25 turns × 3 fixtures = 75 warm reads


def _fixture_manifest(workdir: str) -> dict:
    """sha256 manifest of the fixture copies — records what the run read."""
    manifest = {}
    for name in FIXTURES:
        p = os.path.join(workdir, name)
        with open(p, "rb") as f:
            manifest[name] = hashlib.sha256(f.read()).hexdigest()[:16]
    return manifest


def _wait_for_socket(sock_path: str, timeout: float = 10.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if os.path.exists(sock_path):
            return True
        time.sleep(0.1)
    return False


def _run_gate(turns: int = WARM_TURNS) -> dict:
    """Run cold/warm/invalidation phases against an isolated daemon."""
    workdir = tempfile.mkdtemp(prefix="tr_gate_")
    sock_path = os.path.join(workdir, "sock")
    db_path = os.path.join(workdir, "cache.db")

    # Fixture copies — the gate owns them (it mutates one in Phase C)
    for name in FIXTURES:
        shutil.copy(os.path.join(FIXTURE_SRC, name), os.path.join(workdir, name))
    paths = [os.path.join(workdir, n) for n in FIXTURES]

    env = dict(
        os.environ,
        TOOLRECALL_TRANSPORT=sock_path,
        TOOLRECALL_CACHE_DB=db_path,
        TOOLRECALL_ALLOW_TERMINAL="0",
        PYTHONPATH=os.path.dirname(BENCH_DIR),
    )

    daemon = subprocess.Popen(
        [sys.executable, "-m", "toolrecall", "daemon"],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        if not _wait_for_socket(sock_path):
            raise RuntimeError("gate daemon did not create its socket in time")

        # Point THIS process at the isolated daemon BEFORE re-importing the
        # client. toolrecall.transport resolves DEFAULT_PATH (and config.py
        # resolves the DB path) from os.environ at import time — without
        # this, the gate client would silently connect to the long-running
        # production daemon and the gate would measure the wrong process
        # (all sabotages passed a green gate until this was fixed).
        os.environ["TOOLRECALL_TRANSPORT"] = sock_path
        os.environ["TOOLRECALL_CACHE_DB"] = db_path

        # Re-import with the isolated env (client resolves socket at import)
        for mod in [m for m in list(sys.modules) if m.startswith("toolrecall")]:
            del sys.modules[mod]
        from toolrecall.client import cached_read  # noqa: E402

        # Verify the client is wired to the gate's socket, not production.
        # If this fails, the gate would measure the wrong daemon — abort.
        from toolrecall.transport import DEFAULT_PATH  # noqa: E402

        assert DEFAULT_PATH == sock_path, (
            f"gate client wired to {DEFAULT_PATH!r}, not the isolated socket {sock_path!r} — "
            "the gate would measure the production daemon"
        )

        metrics = {
            "cold_reads": 0,
            "cold_misses": 0,
            "warm_reads": 0,
            "warm_hits": 0,
            "invalidation_miss_after_write": None,
            "invalidation_hit_after_miss": None,
        }

        # ── Phase A: cold — every first read must miss ──
        for p in paths:
            resp = cached_read(p)
            metrics["cold_reads"] += 1
            if not resp.get("cached", False):
                metrics["cold_misses"] += 1

        # ── Phase B: warm — same files, N turns, all must hit ──
        for _ in range(turns):
            for p in paths:
                resp = cached_read(p)
                metrics["warm_reads"] += 1
                if resp.get("cached", False):
                    metrics["warm_hits"] += 1

        # ── Phase C: invalidation — external write must bust the cache ──
        target = paths[0]
        with open(target, "a") as f:
            f.write("\n# gate invalidation probe — content changed\n")
        resp = cached_read(target)
        metrics["invalidation_miss_after_write"] = not resp.get("cached", False)
        resp = cached_read(target)
        metrics["invalidation_hit_after_miss"] = resp.get("cached", False)

        metrics["warm_hit_rate"] = (
            round(metrics["warm_hits"] / metrics["warm_reads"], 4) if metrics["warm_reads"] else 0.0
        )
        metrics["cold_miss_rate"] = (
            round(metrics["cold_misses"] / metrics["cold_reads"], 4)
            if metrics["cold_reads"]
            else 0.0
        )
        metrics["invalidation_ok"] = bool(
            metrics["invalidation_miss_after_write"] and metrics["invalidation_hit_after_miss"]
        )
        metrics["fixture_manifest"] = _fixture_manifest(workdir)
        metrics["turns"] = turns
        return metrics
    finally:
        try:
            daemon.send_signal(signal.SIGTERM)
            daemon.wait(timeout=10)
        except Exception:
            try:
                daemon.kill()
            except Exception:
                pass
        shutil.rmtree(workdir, ignore_errors=True)


def compare(current: dict, baseline: dict) -> list[str]:
    """Return a list of regression descriptions (empty = pass).

    Deterministic setup → tight thresholds. A cache-key change (like the
    cwd-scoped key) invalidates every entry → warm hit rate collapses →
    gate fails. That is CORRECT: key changes require a baseline update PR
    with justification (constitution IX, evidence-first).
    """
    regressions = []
    base_rate = baseline.get("warm_hit_rate", 1.0)
    if current["warm_hit_rate"] < base_rate - 0.02:
        regressions.append(
            f"warm_hit_rate regressed: {current['warm_hit_rate']} < "
            f"baseline {base_rate} (−2pp threshold)"
        )
    if current["cold_miss_rate"] < 0.99:
        regressions.append(
            f"cold reads unexpectedly served from cache: cold_miss_rate="
            f"{current['cold_miss_rate']} (stale pre-seeded DB?)"
        )
    if baseline.get("invalidation_ok") and not current["invalidation_ok"]:
        regressions.append(
            "invalidation broken: external write did not bust the cache "
            f"(miss_after_write={current['invalidation_miss_after_write']}, "
            f"hit_after_miss={current['invalidation_hit_after_miss']})"
        )
    return regressions


def main() -> int:
    parser = argparse.ArgumentParser(description="ToolRecall cache regression gate (A7)")
    parser.add_argument("--turns", type=int, default=WARM_TURNS, help="warm-phase turns")
    parser.add_argument(
        "--update-baseline",
        action="store_true",
        help="Rewrite bench/baseline.json from this run (justify in the PR!)",
    )
    parser.add_argument("--json", action="store_true", help="Machine-readable output only")
    args = parser.parse_args()

    if not os.path.isdir(FIXTURE_SRC):
        print(f"FAIL: fixtures missing: {FIXTURE_SRC}", file=sys.stderr)
        return 2

    metrics = _run_gate(turns=args.turns)

    with open(LAST_RUN_PATH, "w") as f:
        json.dump(metrics, f, indent=2)

    if args.update_baseline:
        baseline = {
            "warm_hit_rate": metrics["warm_hit_rate"],
            "cold_miss_rate": metrics["cold_miss_rate"],
            "invalidation_ok": metrics["invalidation_ok"],
            "turns": metrics["turns"],
            "fixture_manifest": metrics["fixture_manifest"],
        }
        with open(BASELINE_PATH, "w") as f:
            json.dump(baseline, f, indent=2)
            f.write("\n")
        if not args.json:
            print(f"baseline updated: {BASELINE_PATH}")
            print(json.dumps(baseline, indent=2))
        return 0

    if not os.path.exists(BASELINE_PATH):
        print(
            f"FAIL: no baseline at {BASELINE_PATH} — run with --update-baseline first",
            file=sys.stderr,
        )
        return 2

    with open(BASELINE_PATH) as f:
        baseline = json.load(f)

    regressions = compare(metrics, baseline)
    if args.json:
        print(json.dumps({"metrics": metrics, "regressions": regressions}, indent=2))
    else:
        print(
            f"gate: warm_hit_rate={metrics['warm_hit_rate']} "
            f"(baseline {baseline.get('warm_hit_rate')}), "
            f"cold_miss_rate={metrics['cold_miss_rate']}, "
            f"invalidation_ok={metrics['invalidation_ok']}"
        )
        if regressions:
            print("REGRESSIONS:")
            for r in regressions:
                print(f"  ✗ {r}")
        else:
            print("✓ no regression")

    return 1 if regressions else 0


if __name__ == "__main__":
    sys.exit(main())
