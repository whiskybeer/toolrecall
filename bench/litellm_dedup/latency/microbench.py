#!/usr/bin/env python3
"""microbench.py — isolated wall-time cost of dedup_messages per request shape.

Measures the pure overhead the dedup hook adds to the inference request path,
independent of network variance. N iterations per shape via perf_counter_ns(),
reports mean / p50 / p95 / p99 in microseconds.

Usage:
    python3 microbench.py --iters 10000 --out microbench_results.json
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent))
from toolrecall.adapters.litellm import dedup_messages  # noqa: E402

from fixtures import SHAPES  # noqa: E402


def pct(sorted_ns: list[int], q: float) -> float:
    """quantile of a sorted list of integers -> value in microseconds."""
    if not sorted_ns:
        return 0.0
    idx = int(q * (len(sorted_ns) - 1))
    return sorted_ns[idx] / 1000.0  # ns -> us


def bench_shape(name: str, gen, iters: int) -> dict:
    flat: list[int] = []
    for _ in range(iters):
        payload = gen()
        t0 = time.perf_counter_ns()
        dedup_messages(payload)  # defaults: min_chars=800, protect_last=2
        flat.append(time.perf_counter_ns() - t0)
    flat.sort()
    mean_us = statistics.mean(flat) / 1000.0
    n_msgs = len(gen())
    return {
        "shape": name,
        "n_messages": n_msgs,
        "n": len(flat),
        "mean_us": round(mean_us, 3),
        "p50_us": round(pct(flat, 0.50), 3),
        "p95_us": round(pct(flat, 0.95), 3),
        "p99_us": round(pct(flat, 0.99), 3),
        "min_us": round(flat[0] / 1000.0, 3),
        "max_us": round(flat[-1] / 1000.0, 3),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--iters", type=int, default=10_000)
    ap.add_argument("--out", default="microbench_results.json")
    ap.add_argument("--shapes", default="small,dup_heavy,wrap_varied")
    args = ap.parse_args()

    results = []
    print(f"iterations={args.iters}")
    for name in args.shapes.split(","):
        name = name.strip()
        gen = SHAPES[name]
        r = bench_shape(name, gen, args.iters)
        results.append(r)
        print(
            f"  {name:12s} n={r['n']:>6d} "
            f"mean={r['mean_us']:>9.3f}µs p50={r['p50_us']:>9.3f} "
            f"p95={r['p95_us']:>9.3f} p99={r['p99_us']:>9.3f}µs "
            f"(msgs={r['n_messages']})"
        )

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(results, indent=2) + "\n")
    print(f"saved -> {out}")


if __name__ == "__main__":
    main()
