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
  - Tool = HTTP GET to httpbin.org/delay/1 (~1s server-side latency, stable)
    with the seed in the query string → per-seed cache namespaces, no
    cross-seed contamination, no shared-state dependency.
  - Live-call counter wraps the real function — hits NEVER increment it.
  - Arms differ ONLY in ttl. Both run with the daemon up; the script
    preflights daemon_running() and refuses to run treatment without it
    (a silent bypass would fake a null result).

Run (Windows PowerShell, after `toolrecall setup`, from the repo or anywhere
with toolrecall + google-antigravity importable):

    python bench\\antigravity_win.py --arm both --seeds 5

Output: JSON lines on stdout (one per seed×arm) + a human summary.
Send the JSON back — numbers only, no transcripts. Do not commit output.

Requires the ToolRecall daemon running (toolrecall status). All mcp_cache
writes go through the client-side cache DB resolved by the toolrecall config
— same file the daemon/CLI read on the same machine; leave
TOOLRECALL_CACHE_DB unset so both sides agree.
"""

import argparse
import json
import sys
import time
import urllib.request


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
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--arm", choices=["control", "treatment", "both"], default="both")
    ap.add_argument("--seeds", type=int, default=5, help="cache namespaces (default 5)")
    ap.add_argument("--passes", type=int, default=2, help="cold + warm passes (default 2)")
    ap.add_argument("--slots", type=int, default=3, help="distinct args per pass (default 3)")
    ap.add_argument(
        "--url",
        default="https://httpbin.org/delay/1?seed={seed}&slot={slot}",
        help="URL template; {seed}/{slot} are substituted (default: httpbin delay/1 ≈ 1s)",
    )
    ap.add_argument("--timeout", type=float, default=30.0)
    args = ap.parse_args()

    preflight()
    arms = []
    if args.arm in ("control", "both"):
        arms.append(("control", 0))
    if args.arm in ("treatment", "both"):
        arms.append(("treatment", 300))

    results = [
        run_arm(name, ttl, args.seeds, args.passes, args.slots, args.url, args.timeout)
        for name, ttl in arms
    ]

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
