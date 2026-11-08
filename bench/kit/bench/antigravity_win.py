# Copyright (c) 2026 Robin Schultka
# SPDX-License-Identifier: MIT
# Source: https://github.com/whiskybeer/toolrecall
"""Antigravity adapter benchmark — Windows-local, GUI-free, deterministic.

Measures what the Antigravity adapter actually delivers (there is NO billed-
token metric for Antigravity — inference is Google-hosted, no endpoint
override). Currency of this benchmark:

  1. live external calls avoided (ground truth, counted at the function)
  2. wall-clock per call, cold vs warm
  3. decorator overhead when disabled (ttl=0) — the "what NOT to claim" guard

Design:
  - Two arms, equal work: control (ttl=0, cache bypass) and treatment
    (ttl=300). Identical call sequences per seed.
  - Tool = HTTP GET to a LOCAL HTTPS test server (local_server.py, loopback
    only, bundled pinned cert) — zero third-party egress. The /delay/<ms>
    route simulates ~<ms> latency deterministically; seed+slot in the query
    → per-seed cache namespaces, no cross-seed contamination.
  - Live-call counter wraps the real function — hits NEVER increment it.
  - Server-side /counter provides independent ground truth (its count of
    /delay requests should equal the client-side live-call count).
  - Arms differ ONLY in ttl. Both run with the daemon up; the script
    preflights daemon_running() and refuses to run treatment without it
    (a silent bypass would fake a null result).

Run (Windows PowerShell, after `toolrecall setup`, from the kit folder):

    python bench\\antigravity_win.py --arm both --seeds 5

Output: JSON lines on stdout (one per seed×arm) + a human summary.
Send the JSON back — numbers only, no transcripts. Do not commit output.

Requires the ToolRecall daemon running (toolrecall status). All mcp_cache
writes go through the client-side cache DB resolved by the toolrecall config
— same file the daemon/CLI read on the same machine; leave
TOOLRECALL_CACHE_DB unset so both sides agree.

Network: loopback only (127.0.0.1, TLS with the bundled pinned cert).
No third-party service is contacted.
"""

import argparse
import json
import ssl
import sys
import time
import urllib.request
from pathlib import Path

# Local test server (bundled next to this script in the kit).
_KIT_DIR = Path(__file__).resolve().parent
local_server = None  # imported lazily in main() so --help works offline


def _build_tool(ttl: int, url_template: str, timeout: float):
    """Build the (counter, wrapped_tool) pair for one arm."""
    from toolrecall.adapters import antigravity

    counter = {"live": 0}

    def fetch_url(seed: int, slot: int) -> str:
        counter["live"] += 1
        url = url_template.format(seed=seed, slot=slot)
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return f"status={resp.status} bytes={len(resp.read())}"

    wrapped = antigravity.cached_tool(ttl=ttl)(fetch_url)
    return counter, wrapped


def run_arm(arm: str, ttl: int, seeds: int, passes: int, slots: int, url_template: str, timeout: float):
    """One arm: seeds × passes × slots identical-structure calls. JSON-ready."""
    counter, wrapped = _build_tool(ttl, url_template, timeout)
    latencies = []
    t_start = time.perf_counter()
    for seed in range(1, seeds + 1):
        for _pass in range(passes):
            for slot in range(slots):
                t0 = time.perf_counter()
                wrapped(seed=seed, slot=slot)
                latencies.append(round(time.perf_counter() - t0, 4))
    total = round(time.perf_counter() - t_start, 3)
    n = len(latencies)
    warm = latencies[(seeds * slots):] if passes > 1 else []
    cold = latencies[: (seeds * slots)] if passes > 1 else latencies
    return {
        "arm": arm,
        "ttl": ttl,
        "seeds": seeds,
        "passes": passes,
        "slots": slots,
        "total_s": total,
        "calls": n,
        "live_calls": counter["live"],
        "cache_hits": n - counter["live"],
        "cold_avg_s": round(sum(cold) / len(cold), 4) if cold else None,
        "warm_avg_s": round(sum(warm) / len(warm), 4) if warm else None,
        "latencies": latencies,
    }


def preflight() -> None:
    from toolrecall import client as tr_client

    if not tr_client.daemon_running():
        print(
            "ERROR: ToolRecall daemon not running — treatment arm would silently "
            "bypass (fake null result). Start it:  toolrecall daemon",
            file=sys.stderr,
        )
        sys.exit(2)
    print("# preflight: daemon running ✓", file=sys.stderr)


def main() -> None:
    global local_server
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--arm", choices=["control", "treatment", "both"], default="both")
    ap.add_argument("--seeds", type=int, default=5, help="cache namespaces (default 5)")
    ap.add_argument("--passes", type=int, default=2, help="cold + warm passes (default 2)")
    ap.add_argument("--slots", type=int, default=3, help="distinct args per pass (default 3)")
    ap.add_argument(
        "--ms",
        type=int,
        default=1000,
        help="simulated server latency per live call in ms (default 1000)",
    )
    ap.add_argument("--timeout", type=float, default=30.0)
    args = ap.parse_args()

    preflight()

    # Boot the LOCAL test server (zero third-party egress, TLS loopback).
    import importlib.util

    server_path = _KIT_DIR / "local_server.py"
    if not server_path.exists():
        # repo layout: bench/kit/local_server.py next to bench/antigravity_win.py
        server_path = _KIT_DIR / "kit" / "local_server.py"
    spec = importlib.util.spec_from_file_location("local_server", server_path)
    local_server = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(local_server)
    httpd, port = local_server.serve_in_thread(0)
    url_template = f"https://127.0.0.1:{port}/delay/{args.ms}?seed={{seed}}&slot={{slot}}"
    print(f"# local test server: https://127.0.0.1:{port} (loopback TLS, no egress)", file=sys.stderr)

    # Pin the bundled cert — verification ON, just scoped to this CA file.
    client_ctx = ssl.create_default_context(cafile=str(server_path.parent / "local_cert.pem"))

    def fetch(seed: int, slot: int) -> str:
        url = url_template.format(seed=seed, slot=slot)
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, context=client_ctx, timeout=args.timeout) as resp:
            return f"status={resp.status} bytes={len(resp.read())}"

    counters = {"live_total": 0}

    def counted(seed: int, slot: int) -> str:
        counters["live_total"] += 1
        return fetch(seed, slot)

    arms = []
    if args.arm in ("control", "both"):
        arms.append(("control", 0))
    if args.arm in ("treatment", "both"):
        arms.append(("treatment", 300))

    results = []
    for name, ttl in arms:
        # fresh cached_tool per arm — ttl is the ONLY difference
        from toolrecall.adapters import antigravity

        live_before = counters["live_total"]
        wrapped = antigravity.cached_tool(ttl=ttl)(counted)
        latencies = []
        t_start = time.perf_counter()
        # Pass OUTER, seed/slot inner: pass 0 = every call cold (live on
        # treatment), pass 1 = every call warm (cache hit) — the cold/warm
        # slices below then split cleanly. Seed-inner order would interleave
        # seed 2's live calls after seed 1's hits and poison both averages.
        for _pass in range(args.passes):
            for seed in range(1, args.seeds + 1):
                for slot in range(args.slots):
                    t0 = time.perf_counter()
                    wrapped(seed=seed, slot=slot)
                    latencies.append(round(time.perf_counter() - t0, 4))
        total = round(time.perf_counter() - t_start, 3)
        n = len(latencies)
        warm = latencies[(args.seeds * args.slots):] if args.passes > 1 else []
        cold = latencies[: (args.seeds * args.slots)] if args.passes > 1 else latencies
        arm_live = counters["live_total"] - live_before
        results.append(
            {
                "arm": name,
                "ttl": ttl,
                "seeds": args.seeds,
                "passes": args.passes,
                "slots": args.slots,
                "total_s": total,
                "calls": n,
                "live_calls": arm_live,
                "cache_hits": n - arm_live,
                "cold_avg_s": round(sum(cold) / len(cold), 4) if cold else None,
                "warm_avg_s": round(sum(warm) / len(warm), 4) if warm else None,
                "latencies": latencies,
            }
        )

    # Server-side ground truth: total /delay requests the server actually saw.
    counter_ctx = ssl.create_default_context(cafile=str(server_path.parent / "local_cert.pem"))
    seen = json.loads(
        urllib.request.urlopen(
            f"https://127.0.0.1:{port}/counter", context=counter_ctx, timeout=5
        ).read()
    )
    print(f"# server-side /delay request count (ground truth): {seen}", file=sys.stderr)

    for r in results:
        print(json.dumps(r, ensure_ascii=False))

    if len(results) == 2:
        ctrl, treat = results
        print(
            "\n# summary\n"
            f"# live calls:  control={ctrl['live_calls']}  treatment={treat['live_calls']}  "
            f"avoided={ctrl['live_calls'] - treat['live_calls']}\n"
            f"# total wall:  control={ctrl['total_s']}s  treatment={treat['total_s']}s  "
            f"delta={round(ctrl['total_s'] - treat['total_s'], 3)}s\n"
            f"# cold avg:    control={ctrl['cold_avg_s']}s  treatment={treat['cold_avg_s']}s\n"
            f"# warm avg:    control={ctrl['warm_avg_s']}s  treatment={treat['warm_avg_s']}s\n"
            "# NOT measured here (unmeasurable for Antigravity): billed-token savings.",
            file=sys.stderr,
        )


if __name__ == "__main__":
    main()
