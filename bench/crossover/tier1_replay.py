#!/usr/bin/env python3
"""Tier 1 (free, $0): Replay ToolRecall's exact dedup rule over trajectories.

Supports two modes:
  1. --swe-csv PATH   Summarize the SWE-agent 80,036-session corpus CSV
                      (default: ~/workspace/mcp-dup-rate-study/long_session_savings.csv).
  2. --claude-dir DIR Replay the exact dedup rule (MIN_CHARS=800, PROTECT_LAST=2,
                      STUB_LEN=120) over local Claude Code session JSONL files
                      and report repeat_share, stub_bytes_served, and savings
                      by turn-count bucket.

Usage:
  python3 bench/crossover/tier1_replay.py
  python3 bench/crossover/tier1_replay.py --claude-dir ~/.claude/projects
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import statistics as st
from collections import Counter, defaultdict
from pathlib import Path

MIN_CHARS = int(os.environ.get("TOOLRECALL_DEDUP_MIN_CHARS", "800"))
PROTECT_LAST = int(os.environ.get("TOOLRECALL_DEDUP_PROTECT_LAST", "2"))
STUB_LEN = 120
DEFAULT_SWE_CSV = Path.home() / "workspace" / "mcp-dup-rate-study" / "long_session_savings.csv"

BUCKETS = [(1, 20), (21, 40), (41, 80), (81, 150), (151, 250), (251, 10**9)]


def bucket_of(n: int) -> str:
    for lo, hi in BUCKETS:
        if lo <= n <= hi:
            return f"{lo}-{hi if hi < 10**9 else '+'}"
    return "?"


def replay_blocks(blocks: list[tuple[bytes, int]], protect_last: int = PROTECT_LAST) -> dict:
    """Replay the keep-first, protect-last-N dedup rule over (digest, byte_len) blocks."""
    if not blocks:
        return {
            "n_blocks": 0,
            "session_bytes": 0,
            "stub_bytes_served": 0,
            "distinct_block_bytes": 0,
            "repeated_block_bytes": 0,
            "repeat_share": 0.0,
            "pct_saved": 0.0,
        }

    total_base = 0
    total_saved = 0
    dup = 0
    counts: Counter[bytes] = Counter()
    distinct_sizes: dict[bytes, int] = {}
    repeated_bytes = 0
    running = 0

    for k, (dg, sz) in enumerate(blocks, start=1):
        running += sz
        if dg not in distinct_sizes:
            distinct_sizes[dg] = sz
        else:
            repeated_bytes += sz
        # Only blocks[0 : k - protect_last] are stubbable.
        fold_idx = k - protect_last
        if fold_idx >= 1:
            pd, ps = blocks[fold_idx - 1]
            counts[pd] += 1
            if counts[pd] > 1:
                dup += max(0, ps - STUB_LEN)
        total_base += running
        total_saved += dup

    distinct_bytes = sum(distinct_sizes.values())
    repeat_share = (repeated_bytes / distinct_bytes) if distinct_bytes > 0 else 0.0
    pct_saved = (total_saved / total_base * 100.0) if total_base > 0 else 0.0
    return {
        "n_blocks": len(blocks),
        "session_bytes": total_base,
        "stub_bytes_served": total_saved,
        "distinct_block_bytes": distinct_bytes,
        "repeated_block_bytes": repeated_bytes,
        "repeat_share": repeat_share,
        "pct_saved": pct_saved,
    }


def _blocks_from_claude_jsonl(path: Path) -> tuple[int, list[tuple[bytes, int]]]:
    """Extract (n_turns, [(digest, byte_len), ...]) from a Claude Code JSONL."""
    blocks: list[tuple[bytes, int]] = []
    n_turns = 0
    seen_ids: set[str] = set()
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get("isSidechain"):
                continue
            etype = entry.get("type")
            if etype == "assistant":
                mid = (entry.get("message") or {}).get("id")
                if mid and mid not in seen_ids:
                    seen_ids.add(mid)
                    n_turns += 1
            elif etype == "user":
                content = (entry.get("message") or {}).get("content")
                texts: list[str] = []
                if isinstance(content, str) and len(content) >= MIN_CHARS:
                    texts.append(content)
                elif isinstance(content, list):
                    for item in content:
                        if not isinstance(item, dict):
                            continue
                        if item.get("type") == "text":
                            t = item.get("text") or ""
                            if len(t) >= MIN_CHARS:
                                texts.append(t)
                        elif item.get("type") == "tool_result":
                            tc = item.get("content")
                            if isinstance(tc, str) and len(tc) >= MIN_CHARS:
                                texts.append(tc)
                            elif isinstance(tc, list):
                                for sub in tc:
                                    if isinstance(sub, dict) and sub.get("type") == "text":
                                        t = sub.get("text") or ""
                                        if len(t) >= MIN_CHARS:
                                            texts.append(t)
                for txt in texts:
                    b = txt.encode("utf-8", "replace")
                    blocks.append((hashlib.sha256(b).digest(), len(b)))
    return n_turns, blocks


def summarize_swe_csv(csv_path: Path) -> None:
    if not csv_path.exists():
        print(f"SWE-agent CSV not found at {csv_path}; skipping corpus summary.")
        return
    agg = defaultdict(lambda: {"n": 0, "chars": 0, "saved": 0, "pcts": []})
    tot_n = tot_c = tot_s = 0
    with open(csv_path, newline="") as fh:
        for row in csv.DictReader(fh):
            nb = int(row["n_blocks"])
            sc = int(row["session_chars"])
            sv = int(row["saved_chars"])
            b = bucket_of(nb)
            agg[b]["n"] += 1
            agg[b]["chars"] += sc
            agg[b]["saved"] += sv
            agg[b]["pcts"].append(float(row["pct"]))
            tot_n += 1
            tot_c += sc
            tot_s += sv

    print(f"## Tier 1 — SWE-agent corpus replay ({csv_path.name}, n={tot_n:,} sessions)")
    print(f"  {'blocks':>10} {'sessions':>10} {'mean_saved%':>12} {'median_saved%':>14}")
    for lo, hi in BUCKETS:
        b = f"{lo}-{hi if hi < 10**9 else '+'}"
        d = agg[b]
        if not d["n"]:
            continue
        weighted = 100.0 * d["saved"] / d["chars"] if d["chars"] else 0.0
        med = st.median(d["pcts"])
        print(f"  {b:>10} {d['n']:>10,} {weighted:>11.1f}% {med:>13.1f}%")
    overall = 100.0 * tot_s / tot_c if tot_c else 0.0
    print(f"  {'OVERALL':>10} {tot_n:>10,} {overall:>11.1f}%")


def replay_claude_dir(claude_dir: Path) -> None:
    files = sorted(claude_dir.rglob("*.jsonl"))
    if not files:
        print(f"No .jsonl sessions found under {claude_dir}")
        return
    agg = defaultdict(list)
    for f in files:
        n_turns, blocks = _blocks_from_claude_jsonl(f)
        if not blocks:
            continue
        res = replay_blocks(blocks)
        res["n_turns"] = n_turns
        agg[bucket_of(n_turns or res["n_blocks"])].append(res)

    print(f"\n## Tier 1 — Local Claude Code sessions replay ({claude_dir})")
    print(f"  {'turns':>10} {'sessions':>10} {'med_repeat_share':>17} {'med_saved%':>11} {'med_distinct_KB':>16}")
    for lo, hi in BUCKETS:
        b = f"{lo}-{hi if hi < 10**9 else '+'}"
        rows = agg[b]
        if not rows:
            continue
        med_rs = st.median(r["repeat_share"] for r in rows)
        med_pct = st.median(r["pct_saved"] for r in rows)
        med_kb = st.median(r["distinct_block_bytes"] for r in rows) / 1024.0
        print(f"  {b:>10} {len(rows):>10} {med_rs:>17.3f} {med_pct:>10.1f}% {med_kb:>15.1f}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Tier 1 free trajectory replay for ToolRecall crossover")
    ap.add_argument("--swe-csv", type=Path, default=DEFAULT_SWE_CSV)
    ap.add_argument("--claude-dir", type=Path, default=None)
    args = ap.parse_args()
    summarize_swe_csv(args.swe_csv)
    if args.claude_dir:
        replay_claude_dir(args.claude_dir)


if __name__ == "__main__":
    main()
