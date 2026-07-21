#!/usr/bin/env python3
"""interleave.py — three-arm interleaved benchmark driver.

Runs arms in rotation: naive → prefix → toolrecall → naive → ...
Two-arm comparison: full history (naive) vs context dropping (toolrecall).

Usage:
    /tmp/bench-env/bin/python3 bench/interleave.py <workload> [--seeds 3] [--max-turns 500] [--provider PROVIDER] [--model MODEL] [--delay SECS] [--dry-run]

Flags:
    --dry-run    Skip LLM calls — tests the plumbing without spending money
    --seeds N    Run each arm with N different seeds (default 3, for variance)
    --max-turns  Max turns per run (default 500)
    --provider   LLM provider: openrouter (default) or anthropic
    --model      Model override (uses provider default if unset)
    --delay      Seconds between turns to avoid rate limits (default: 0.0)

Output:
    - Writes turn_log rows to benchmark.db
    - Prints run_ids at the end for analyze.py consumption
    - Prints a quick summary table
"""

import argparse
import os
import sys
import time

# Ensure bench/ is importable
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from run_arm import run_arm

ARMS = ["naive", "prefix", "toolrecall"]


def main():
    parser = argparse.ArgumentParser(
        description="Three-arm interleaved benchmark driver"
    )
    parser.add_argument("workload", nargs="?", default="bugfix",
                        help="Workload: bugfix, feature, analysis")
    parser.add_argument("--seeds", type=int, default=3,
                        help="Number of seeds per arm (default 3)")
    parser.add_argument("--max-turns", type=int, default=500)
    parser.add_argument("--provider", default="openrouter",
                        choices=["openrouter", "anthropic"],
                        help="LLM provider (default: openrouter)")
    parser.add_argument("--model", default=None,
                        help="Model name override (uses provider default if unset)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Skip LLM calls — dry-run only")
    parser.add_argument("--delay", type=float, default=0.0,
                        help="Seconds between turns to avoid rate limits (default: 0.0)")
    args = parser.parse_args()

    print(f"Interleaved benchmark: {args.workload}")
    print(f"  Arms: {', '.join(ARMS)}")
    print(f"  Seeds per arm: {args.seeds}")
    print(f"  Max turns: {args.max_turns}")
    print(f"  Provider: {args.provider}")
    print(f"  Model: {args.model or '(default)'}")
    print(f"  Delay: {args.delay}s")
    print(f"  Dry run: {args.dry_run}")
    print()

    run_ids = []

    for round_num in range(args.seeds):
        for arm in ARMS:
            seed = 42 + round_num * 10  # same seed for all arms in this round
            label = f"{arm}/{args.workload}/s{seed}"

            print(f"[{round_num + 1}/{args.seeds}] {label}  ", end="", flush=True)
            t0 = time.time()

            rid = run_arm(
                arm=arm,
                workload_id=args.workload,
                seed=seed,
                max_turns=args.max_turns,
                dry_run=args.dry_run,
                provider=args.provider,
                model=args.model,
                delay=args.delay,
            )
            elapsed = time.time() - t0

            run_ids.append((arm, args.workload, seed, rid, round(elapsed, 1)))
            print(f"done in {elapsed:.0f}s  run_id={rid[:8]}...")

    # Print summary
    print()
    print("=" * 60)
    print("SUMMARY")
    print("=" * 60)
    print(f"{'Arm':12s} {'Workload':10s} {'Seed':>5} {'Turns':>6} {'Time':>7}  Run ID")
    print("-" * 60)

    import sqlite3
    con = sqlite3.connect(os.path.expanduser("~/.toolrecall/benchmark.db"))
    for arm, wl, seed, rid, et in run_ids:
        turn_count = con.execute(
            "SELECT COUNT(*) FROM turn_log WHERE run_id = ?", (rid,)
        ).fetchone()[0]
        print(f"{arm:12s} {wl:10s} {seed:>5} {turn_count:>6} {et:>6.0f}s  {rid}")
    con.close()

    print()
    print("To generate charts and stats:")
    print(f"  /tmp/bench-env/bin/python3 bench/analyze.py")
    print()
    print("Run IDs (for direct query):")
    for _, _, _, rid, _ in run_ids:
        print(f"  {rid}")


if __name__ == "__main__":
    main()