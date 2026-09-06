#!/usr/bin/env python3
# Copyright (c) 2026 Robin Schultka
# SPDX-License-Identifier: MIT
"""tr-warp-stats — identical-request fraction of any proxy usage log.

Reads a ToolRecall proxy usage CSV (or any CSV with the same columns) and
reports the caching upper bound: what fraction of requests (and billed tokens)
were byte-identical to a request seen before — i.e. what a response cache
would have served for free.

This is the "5-minute experiment" from the Warp pitch: run it against any
inference log to get one number that decides whether ToolRecall pays off.

Usage:
    python3 scripts/tr_warp_stats.py [--csv PATH] [--window SECONDS] [--json]

Metrics:
  repeat_request_ratio   rows whose request_hash appeared >1x / total rows
  repeat_token_share     prompt tokens on repeatable rows / total prompt tokens
  windowed variant       same, but only repeats within --window seconds
                         (fleet re-use happens inside work windows; a repeat
                         3 weeks later is worthless to a TTL-bounded cache)
"""

from __future__ import annotations

import argparse
import csv
import json
from collections import Counter, defaultdict


def load_rows(path: str) -> list[dict]:
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def analyze(rows: list[dict], window: float | None = None) -> dict:
    real = [
        r
        for r in rows
        if r.get("cache_status") != "TEST"
        and r.get("target_host") != "test.host"
        # chat-completions only: /models polling repeats forever but carries
        # no billed tokens and is not what a response cache is for
        and "chat/completions" in (r.get("target_path") or "")
    ]
    n = len(real)
    if n == 0:
        return {"error": "no data rows"}

    total_tokens = sum(int(r.get("prompt_tokens") or 0) for r in real)

    # global identical-hash analysis
    hash_counts = Counter(r["request_hash"] for r in real)
    repeat_rows = [r for r in real if hash_counts[r["request_hash"]] > 1]
    repeat_tokens = sum(int(r.get("prompt_tokens") or 0) for r in repeat_rows)

    out = {
        "rows_analyzed": n,
        "unique_request_hashes": len(hash_counts),
        "repeat_request_ratio": round(len(repeat_rows) / n, 4),
        "repeat_token_share": round(repeat_tokens / total_tokens, 4) if total_tokens else 0.0,
        "cacheable_cost_fraction_upper_bound": round(repeat_tokens / total_tokens, 4)
        if total_tokens
        else 0.0,
    }

    # windowed analysis: repeat must occur within `window` seconds of a prior
    # identical request (fleet re-use is temporal)
    if window:
        by_hash: dict[str, list[float]] = defaultdict(list)
        for r in real:
            by_hash[r["request_hash"]].append(float(r["timestamp"]))
        window_repeat_rows = 0
        window_repeat_tokens = 0
        for r in real:
            t = float(r["timestamp"])
            if any(0 < t - prev <= window for prev in by_hash[r["request_hash"]] if prev < t):
                window_repeat_rows += 1
                window_repeat_tokens += int(r.get("prompt_tokens") or 0)
        out["window_seconds"] = window
        out["window_repeat_request_ratio"] = round(window_repeat_rows / n, 4)
        out["window_repeat_token_share"] = (
            round(window_repeat_tokens / total_tokens, 4) if total_tokens else 0.0
        )
        out["cacheable_cost_fraction_upper_bound"] = out["window_repeat_token_share"]

    out["top_repeats"] = [
        {"request_hash": h[:16], "count": c} for h, c in hash_counts.most_common(5) if c > 1
    ]

    # per-host split
    per_host: dict[str, dict] = {}
    for host in {r["target_host"] for r in real}:
        rows_h = [r for r in real if r["target_host"] == host]
        tok_h = sum(int(r.get("prompt_tokens") or 0) for r in rows_h)
        rep_h = sum(
            int(r.get("prompt_tokens") or 0) for r in rows_h if hash_counts[r["request_hash"]] > 1
        )
        per_host[host] = {
            "prompt_tokens": tok_h,
            "repeat_token_share": round(rep_h / tok_h if tok_h else 0.0, 4),
        }
    out["per_host"] = per_host
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--csv", default=os.path.expanduser("~/.toolrecall/proxy_usage.csv"),
                    help="Proxy usage CSV (default: ~/.toolrecall/proxy_usage.csv)")
    ap.add_argument(
        "--window", type=float, default=3600, help="repeat window seconds (default 1h; 0 = disable)"
    )
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    result = analyze(load_rows(args.csv), window=args.window if args.window > 0 else None)

    if args.json:
        print(json.dumps(result, indent=2))
        return 0

    print("=" * 64)
    print("  tr-warp-stats — caching upper bound from real traffic")
    print("=" * 64)
    print(f"  rows analyzed:         {result['rows_analyzed']:,}")
    print(f"  unique request hashes: {result['unique_request_hashes']:,}")
    print(f"  repeat request ratio:  {result['repeat_request_ratio'] * 100:.1f}%")
    print(f"  repeat token share:    {result['repeat_token_share'] * 100:.1f}%")
    if "window_repeat_token_share" in result:
        print(
            f"  within {result['window_seconds']:.0f}s window: "
            f"{result['window_repeat_token_share'] * 100:.1f}% of tokens cacheable"
        )
    print(
        f"  → savings upper bound: {result['cacheable_cost_fraction_upper_bound'] * 100:.1f}% of inference spend"
    )
    for tr in result["top_repeats"]:
        print(f"    hash {tr['request_hash']}… ×{tr['count']}")
    for h, stats in result["per_host"].items():
        print(
            f"    {h}: {stats['prompt_tokens']:,} tok, repeat share {stats['repeat_token_share'] * 100:.1f}%"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
