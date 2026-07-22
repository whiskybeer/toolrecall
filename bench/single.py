#!/usr/bin/env python3
"""Single-arm benchmark runner — one arm per invocation, clean slate.

Usage:
  # Run toolrecall arm, 2 seeds, 100 turns (in its own session)
  cd ~/toolrecall && /tmp/bench-env/bin/python3 bench/single.py toolrecall analysis 2 100

  # Then in a FRESH session (or after clearing cache):
  cd ~/toolrecall && /tmp/bench-env/bin/python3 bench/single.py naive analysis 2 100

Arms run in isolation — no cross-contamination.
"""

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from run_arm import run_arm


ARMS = ["naive", "toolrecall"]
WORKLOADS = ["bugfix", "feature", "analysis", "review"]


def main():
    parser = argparse.ArgumentParser(description="Single-arm benchmark runner")
    parser.add_argument("arm", choices=ARMS, help="Arm to run")
    parser.add_argument("workload", nargs="?", default="analysis",
                        help=f"Workload: {', '.join(WORKLOADS)}")
    parser.add_argument("seeds", type=int, nargs="?", default=2,
                        help="Number of seeds (default 2)")
    parser.add_argument("max_turns", type=int, nargs="?", default=100,
                        help="Max turns per run (default 100)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Skip LLM calls — test plumbing only")
    parser.add_argument("--provider", default="openrouter",
                        choices=["openrouter", "anthropic"],
                        help="LLM provider (default: openrouter)")
    parser.add_argument("--model", default=None,
                        help="Model name override (uses provider default if unset)")
    parser.add_argument("--delay", type=float, default=0.0,
                        help="Seconds between turns to avoid rate limits (default: 0.0)")
    args = parser.parse_args()

    print(f"Single-arm benchmark: {args.arm}")
    print(f"  Workload: {args.workload}")
    print(f"  Seeds: {args.seeds}")
    print(f"  Max turns: {args.max_turns}")
    print(f"  Provider: {args.provider}")
    print(f"  Model: {args.model or '(default)'}")
    print(f"  Delay: {args.delay}s")
    print(f"  Dry run: {args.dry_run}")
    print(f"  Note: Runs {args.seeds} x {args.arm} in THIS session only.")
    print(f"  Run other arms in separate sessions for clean isolation.")
    print()

    run_ids = []

    for seed_num in range(1, args.seeds + 1):
        seed = 42 + seed_num * 10 + hash(args.arm) % 1000
        label = f"{args.arm}/{args.workload}/s{seed}"

        print(f"[{seed_num}/{args.seeds}] {label}  ", end="", flush=True)
        t0 = time.time()

        rid = run_arm(
            arm=args.arm,
            workload_id=args.workload,
            seed=seed,
            max_turns=args.max_turns,
            dry_run=args.dry_run,
            provider=args.provider,
            model=args.model,
            delay=args.delay,
        )

        elapsed = time.time() - t0
        run_ids.append((args.arm, args.workload, seed, rid, round(elapsed, 1)))
        print(f"done in {elapsed:.0f}s  run_id={rid[:8]}...")

    # Summary — query each per-run DB individually
    import sqlite3
    bench_dir = os.path.expanduser("~/.toolrecall/bench-runs")
    print()
    print("=" * 60)
    print("SUMMARY")
    print("=" * 60)
    print(f"{'Arm':12s} {'Workload':10s} {'Seed':>5} {'Turns':>5} {'Time':>7}  Run ID")
    print("-" * 60)
    for arm, wl, seed, rid, et in run_ids:
        db_path = os.path.join(bench_dir, f"{rid}.db")
        con = sqlite3.connect(db_path) if os.path.exists(db_path) else None
        turn_count = con.execute(
            "SELECT COUNT(*) FROM turn_log WHERE run_id = ?", (rid,)
        ).fetchone()[0] if con else 0
        if con: con.close()
        print(f"{arm:12s} {wl:10s} {seed:>5} {turn_count:>5} {et:>6.0f}s  {rid}")

    print()
    print("To generate charts:")
    print(f"  /tmp/bench-env/bin/python3 bench/analyze.py")


if __name__ == "__main__":
    main()