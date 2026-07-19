"""ToolRecall Proxy Usage Analyzer — query proxy_usage.csv for actual token metrics.

Usage:
    python3 scripts/proxy_usage_query.py                # Summary (default)
    python3 scripts/proxy_usage_query.py --by-status     # Per cache status
    python3 scripts/proxy_usage_query.py --recent N      # Last N entries
    python3 scripts/proxy_usage_query.py --csv           # Full CSV dump
"""

import csv
import os
import sys
from collections import defaultdict

USAGE_LOG = os.path.expanduser("~/.toolrecall/proxy_usage.csv")


def load_rows(limit: int = None) -> list[dict]:
    """Load usage log rows as dicts."""
    if not os.path.exists(USAGE_LOG):
        print(f"No usage log found at {USAGE_LOG}")
        print("The proxy must be actively running with API requests flowing through it.")
        sys.exit(1)
    rows = []
    with open(USAGE_LOG) as f:
        reader = csv.DictReader(f)
        for i, row in enumerate(reader):
            if limit and i >= limit:
                break
            rows.append(row)
    return rows


def fmt(n: int) -> str:
    """Format integer with commas."""
    return f"{n:,}"


def show_summary(rows: list[dict]):
    """Print aggregated summary."""
    total_prompt = 0
    total_completion = 0
    total_cache_read = 0
    total_cache_write = 0
    by_status: dict[str, dict] = defaultdict(
        lambda: {"prompt_tokens": 0, "completion_tokens": 0, "cache_read": 0, "cache_write": 0, "count": 0}
    )

    for r in rows:
        st = r.get("cache_status", "?")
        pt = int(r.get("prompt_tokens", 0) or 0)
        ct = int(r.get("completion_tokens", 0) or 0)
        cr = int(r.get("cache_read_tokens", 0) or 0)
        cw = int(r.get("cache_write_tokens", 0) or 0)
        total_prompt += pt
        total_completion += ct
        total_cache_read += cr
        total_cache_write += cw
        by_status[st]["prompt_tokens"] += pt
        by_status[st]["completion_tokens"] += ct
        by_status[st]["cache_read"] += cr
        by_status[st]["cache_write"] += cw
        by_status[st]["count"] += 1

    print("=" * 65)
    print("  ToolRecall Forward Proxy — Usage Measurement")
    print("=" * 65)
    print(f"\n  Total rows:        {fmt(len(rows))}")
    print(f"  Prompt tokens:     {fmt(total_prompt)}")
    print(f"  Completion tokens: {fmt(total_completion)}")
    print(f"  Cache read tokens: {fmt(total_cache_read)}  (provider prefix caching)")
    print(f"  Cache write tokens:{fmt(total_cache_write)}  (provider prefix cache write)")
    print()

    print(f"  {'Status':<8} {'Count':<8} {'Prompt Tokens':<16} {'Completion':<14} {'Cache Read':<14} {'Cache Write'}")
    print(f"  {'-'*8} {'-'*8} {'-'*16} {'-'*14} {'-'*14} {'-'*12}")
    for status in ["HIT", "MISS", "STREAM"]:
        s = by_status.get(status)
        if s and s["count"] > 0:
            print(f"  {status:<8} {fmt(s['count']):<8} {fmt(s['prompt_tokens']):<16} {fmt(s['completion_tokens']):<14} {fmt(s['cache_read']):<14} {fmt(s['cache_write']):<12}")

    # Other/unknown statuses
    others = {k: v for k, v in by_status.items() if k not in ("HIT", "MISS", "STREAM")}
    if others:
        for status, s in others.items():
            print(f"  {status:<8} {fmt(s['count']):<8} {fmt(s['prompt_tokens']):<16} {fmt(s['completion_tokens']):<14} {fmt(s['cache_read']):<14} {fmt(s['cache_write']):<12}")

    # Actual tokens sent to LLM (MISS + STREAM)
    sent = by_status.get("MISS", {}).get("prompt_tokens", 0) + by_status.get("STREAM", {}).get("prompt_tokens", 0)
    saved = by_status.get("HIT", {}).get("prompt_tokens", 0)
    total = sent + saved
    if total > 0:
        print(f"\n  Actual tokens sent to LLM:  {fmt(sent)} ({sent/total*100:.1f}%)")
        print(f"  Tokens saved by proxy:      {fmt(saved)} ({saved/total*100:.1f}%)")
    print()


def show_by_status(rows: list[dict]):
    """Print per-status breakdown as a compact table."""
    import io
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["timestamp", "status", "host", "path", "prompt_tokens", "completion_tokens", "cache_read", "cache_write"])
    prev = None
    for r in rows:
        st = r.get("cache_status", "?")
        if st != prev:
            if prev is not None:
                output.write("\n")
            prev = st
            writer.writerow([f"--- {st} ({sum(1 for x in rows if x.get('cache_status')==st)} rows) ---"])
        writer.writerow([
            r.get("timestamp", ""),
            st,
            r.get("target_host", ""),
            r.get("target_path", ""),
            r.get("prompt_tokens", ""),
            r.get("completion_tokens", ""),
            r.get("cache_read_tokens", ""),
            r.get("cache_write_tokens", ""),
        ])
    print(output.getvalue())


def show_recent(rows: list[dict], n: int = 10):
    """Show the N most recent entries."""
    recent = rows[-n:] if len(rows) >= n else rows
    print(f"Last {len(recent)} proxy requests:")
    print(f"  {'ts':<12} {'status':<7} {'host':<20} {'path':<28} {'ptok':<8} {'ctok':<8} {'cread':<8}")
    print(f"  {'-'*12} {'-'*7} {'-'*20} {'-'*28} {'-'*8} {'-'*8} {'-'*8}")
    for r in recent:
        ts = r.get("timestamp", "")
        if ts and "." in ts:
            ts = ts.split(".")[0]  # trim subsecond
        print(f"  {ts:<12} {r.get('cache_status','?'):<7} {r.get('target_host',''):<20} {r.get('target_path',''):<28} {r.get('prompt_tokens',''):<8} {r.get('completion_tokens',''):<8} {r.get('cache_read_tokens',''):<8}")
    print()


def show_csv(rows: list[dict]):
    """Dump full CSV to stdout."""
    with open(USAGE_LOG) as f:
        print(f.read().strip())


if __name__ == "__main__":
    rows = load_rows()
    if not rows:
        print("No usage data recorded yet.")
        sys.exit(0)

    if "--csv" in sys.argv:
        show_csv(rows)
    elif "--by-status" in sys.argv:
        show_by_status(rows)
    elif "--recent" in sys.argv:
        idx = sys.argv.index("--recent") + 1
        n = int(sys.argv[idx]) if idx < len(sys.argv) and sys.argv[idx].isdigit() else 10
        show_recent(rows, n)
    else:
        show_summary(rows)
