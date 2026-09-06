#!/usr/bin/env python3
"""Post-run analysis for the Warp eval-replay benchmark.

Reads proxy_usage.csv (epoch window) + edge capture index and produces the
metrics table from the benchmark design:
- replay rate per pass (HIT/STREAM counts + tokens)
- replay rate by turn index (decay curve)
- prompt tokens not recomputed
- Pass@1 delta support: drift check (distinct content_sha per replay slot)

Usage:
  python3 bench/analyze_eval_replay.py --start <epoch> --end <epoch> \
      [--capture-dir /tmp/edge_capture_prod] [--out results/eval_replay_<ts>.json]
"""
import argparse
import csv
import json
import os
from collections import defaultdict

USAGE_CSV = os.path.expanduser("~/.toolrecall/proxy_usage.csv")


def load_window(start: float, end: float):
    rows = []
    with open(USAGE_CSV) as f:
        rd = csv.reader(f)
        next(rd)  # header
        for r in rd:
            try:
                ts = float(r[0])
            except ValueError:
                continue
            if start <= ts <= end:
                rows.append(
                    {
                        "ts": ts,
                        "status": r[1],
                        "host": r[2],
                        "hash": r[4],
                        "prompt": int(r[5]),
                        "completion": int(r[6]),
                    }
                )
    rows.sort(key=lambda x: x["ts"])
    return rows


def load_capture_seq(capture_dir: str):
    """Map proxy request_hash -> capture seq via index.jsonl sha256? Not 1:1
    (edge sha = raw body hash, proxy hash = canon hash). Instead return the
    ordered capture list for timeline segmentation."""
    idx = []
    p = os.path.join(capture_dir, "index.jsonl")
    if not os.path.exists(p):
        return idx
    with open(p) as f:
        for line in f:
            try:
                idx.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return idx


def segment_runs(rows, idle_gap=90.0):
    """Split the request timeline into runs by idle gaps (fresh sessions)."""
    runs, cur = [], []
    for r in rows:
        if cur and r["ts"] - cur[-1]["ts"] > idle_gap:
            runs.append(cur)
            cur = []
        cur.append(r)
    if cur:
        runs.append(cur)
    return runs


def analyze(rows, runs):
    total = len(rows)
    hits = [r for r in rows if r["status"] == "HIT"]
    live = [r for r in rows if r["status"] in ("STREAM", "MISS")]
    tokens_saved = sum(h["prompt"] for h in hits)
    tokens_live = sum(r["prompt"] for r in live)

    # Decay curve: HIT fraction by position within each run
    by_pos = defaultdict(lambda: [0, 0])  # pos -> [hit, total]
    for run in runs:
        for i, r in enumerate(run, 1):
            by_pos[i][1] += 1
            if r["status"] == "HIT":
                by_pos[i][0] += 1

    decay = [
        {"turn_index": pos, "hit": v[0], "total": v[1],
         "rate": round(v[0] / v[1], 3) if v[1] else 0.0}
        for pos, v in sorted(by_pos.items())
    ]

    return {
        "total_requests": total,
        "live_requests": len(live),
        "hit_requests": len(hits),
        "replay_rate": round(len(hits) / total, 3) if total else 0.0,
        "prompt_tokens_live": tokens_live,
        "prompt_tokens_saved": tokens_saved,
        "tokens_saved_pct": round(tokens_saved / (tokens_saved + tokens_live), 3)
        if (tokens_saved + tokens_live) else 0.0,
        "runs_detected": len(runs),
        "decay_curve": decay,
        "per_run": [
            {
                "start": run[0]["ts"],
                "end": run[-1]["ts"],
                "requests": len(run),
                "hits": sum(1 for r in run if r["status"] == "HIT"),
                "prompt_live": sum(r["prompt"] for r in run if r["status"] != "HIT"),
                "prompt_saved": sum(r["prompt"] for r in run if r["status"] == "HIT"),
            }
            for run in runs
        ],
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", type=float, required=True)
    ap.add_argument("--end", type=float, required=True)
    ap.add_argument("--capture-dir", default="/tmp/edge_capture_prod")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    rows = load_window(args.start, args.end)
    runs = segment_runs(rows)
    result = analyze(rows, runs)
    result["window"] = {"start": args.start, "end": args.end}

    out = args.out or f"bench/results/eval_replay_{int(args.start)}.json"
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w") as f:
        json.dump(result, f, indent=1)
    print(json.dumps(result, indent=1))
    print(f"\nsaved: {out}")


if __name__ == "__main__":
    main()
