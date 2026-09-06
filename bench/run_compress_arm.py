#!/usr/bin/env python3
"""run_compress_arm.py — run the hermes_compress arm on large-review.

Faithful reproduction config (matches nocache-gemma-large, July 2026):
  - model google/gemma-3-12b-it via OpenRouter (no prefix caching)
  - BENCH_CONTEXT_LIMIT=128000 (128K budget)
  - 50 turns, seed 42
  - compressor fixed: threshold 0.25, protect_last 6

Writes bench-runs/<run_id>.db then consolidates to
bench-runs/nocache-gemma-compress-s42.db (named, non-overwriting).

Usage:
    python3 run_compress_arm.py [--max-turns N] [--dry-run]
"""

import argparse
import os
import shutil
import sys
import time

BENCH = os.path.expanduser("~/toolrecall/bench")
REPO = os.path.expanduser("~/toolrecall")
RUNS = os.path.expanduser("~/.toolrecall/bench-runs")
os.makedirs(RUNS, exist_ok=True)
sys.path.insert(0, BENCH)
sys.path.insert(0, REPO)

MODEL = "google/gemma-3-12b-it"
TARGET_DB = os.path.join(RUNS, "nocache-gemma-compress-s42.db")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-turns", type=int, default=50)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    os.environ["BENCH_CONTEXT_LIMIT"] = "128000"

    import agent
    import hermes_arms

    hermes_arms.register_hermes_arms(agent)

    from run_arm import run_arm

    print(f"MODEL: {MODEL}  max_turns={args.max_turns}  dry={args.dry_run}  "
          f"context_limit=128000  target_db={TARGET_DB}", flush=True)
    t0 = time.time()
    rid = run_arm(
        arm="hermes_compress",
        workload_id="large-review",
        seed=42,
        max_turns=args.max_turns,
        dry_run=args.dry_run,
        provider="openrouter",
        model=MODEL,
        delay=0.3,
    )
    elapsed = time.time() - t0
    src = os.path.join(RUNS, f"{rid}.db")
    print(f"\nRun complete: {rid} in {elapsed:.0f}s", flush=True)
    if os.path.exists(src):
        shutil.copyfile(src, TARGET_DB)
        print(f"Consolidated -> {TARGET_DB}", flush=True)
    else:
        print(f"WARNING: source db {src} not found", flush=True)


if __name__ == "__main__":
    main()