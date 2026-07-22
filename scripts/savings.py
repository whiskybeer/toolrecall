#!/usr/bin/env python3
"""
toolrecall-savings — Honest API token savings report from proxy logs.

Reads ~/.toolrecall/proxy_usage.csv and reports what ToolRecall actually saved:
  - Real provider-billed tokens (MISS + STREAM from proxy)
  - Cache HIT tokens that never reached the provider
  - Provider prefix-cache metrics (if reported)
  - Dollar estimates at configured pricing

Usage:
    ./scripts/savings.py                    # Full report
    ./scripts/savings.py --today            # Since midnight
    ./scripts/savings.py --since '2026-07-20 12:00:00'
    ./scripts/savings.py --watch            # Tail mode — follow CSV as it grows
    ./scripts/savings.py --json             # Machine-readable output
"""

import csv
import datetime
import os
import sys
import time

# ── Config ────────────────────────────────────────────────────

CSV_PATH = os.path.expanduser("~/.toolrecall/proxy_usage.csv")

# Default pricing (DeepSeek V4 Flash on OpenRouter)
# Override with env vars: TOOLRECALL_PRICE_INPUT, TOOLRECALL_PRICE_OUTPUT (per M tokens)
INPUT_PRICE = float(os.environ.get("TOOLRECALL_PRICE_INPUT", "0.14"))
OUTPUT_PRICE = float(os.environ.get("TOOLRECALL_PRICE_OUTPUT", "0.42"))


# ── Load & filter ─────────────────────────────────────────────

def load_csv(path: str) -> list[dict]:
    """Load proxy_usage.csv, return list of dicts."""
    if not os.path.exists(path):
        print(f"ERROR: {path} not found")
        print("The forward proxy has not logged any requests yet.")
        print("Make sure the proxy is running and routing API calls through it.")
        sys.exit(1)

    rows = []
    with open(path) as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row.get("cache_status") == "TEST":
                continue
            rows.append({
                "ts": float(row["timestamp"]),
                "status": row["cache_status"],
                "host": row["target_host"],
                "path": row["target_path"],
                "prompt": int(row.get("prompt_tokens", 0) or 0),
                "completion": int(row.get("completion_tokens", 0) or 0),
                "cache_read": int(row.get("cache_read_tokens", 0) or 0),
                "cache_write": int(row.get("cache_write_tokens", 0) or 0),
            })
    return rows


def filter_since(rows: list[dict], since_ts: float) -> list[dict]:
    return [r for r in rows if r["ts"] >= since_ts]


# ── Report ─────────────────────────────────────────────────────

def report(rows: list[dict], label: str = ""):
    """Print a report for a set of proxy rows."""

    total = len(rows)
    hits = [r for r in rows if r["status"] == "HIT"]
    misses = [r for r in rows if r["status"] == "MISS"]
    streams = [r for r in rows if r["status"] == "STREAM"]

    # Real billed tokens (MISS + STREAM)
    billed_prompt = sum(r["prompt"] for r in misses + streams)
    billed_completion = sum(r["completion"] for r in misses + streams)
    billed = billed_prompt + billed_completion

    # Tokens saved by cache
    saved_prompt = sum(r["prompt"] for r in hits)
    saved_completion = sum(r["completion"] for r in hits)

    # Provider prefix-cache metrics (from usage block)
    provider_cache_read = sum(r["cache_read"] for r in rows)
    provider_cache_write = sum(r["cache_write"] for r in rows)

    # Provider prefix-cache discount — OpenRouter reports cache_read_input_tokens
    # which means those tokens were billed at the discounted rate.
    # We can't know the exact discount from the log alone, but we can note them.
    provider_cache_estimated_savings = provider_cache_read * 0.14 / 1_000_000 * 0.75  # rough: 75% of input price

    # Cost calculations
    input_cost = billed_prompt / 1_000_000 * INPUT_PRICE
    output_cost = billed_completion / 1_000_000 * OUTPUT_PRICE
    saved_cost = (saved_prompt / 1_000_000 * INPUT_PRICE +
                  saved_completion / 1_000_000 * OUTPUT_PRICE)

    # Time span
    if rows:
        first_dt = datetime.datetime.fromtimestamp(rows[0]["ts"])
        last_dt = datetime.datetime.fromtimestamp(rows[-1]["ts"])
        span = last_dt - first_dt
    else:
        span = datetime.timedelta(0)

    # ── Print ─────────────────────────────────
    if label:
        print(f"=== {label} ===")
    else:
        print(f"=== ToolRecall Proxy Savings ===")
    print()

    time_str = f"{last_dt.strftime('%Y-%m-%d %H:%M')} over {span}"
    if span.total_seconds() < 3600:
        time_str = f"{last_dt.strftime('%Y-%m-%d %H:%M')} over {span.seconds // 60}m"
    elif span.total_seconds() < 86400:
        time_str = f"{last_dt.strftime('%Y-%m-%d %H:%M')} over {span.seconds // 3600}h"
    print(f"  Period:     {time_str}")
    print()

    print(f"  Requests:")
    print(f"    Total:    {total:>6}")
    print(f"    MISS:     {len(misses):>6} ({len(misses)/total*100:.1f}%)" if total else "")
    print(f"    STREAM:   {len(streams):>6} ({len(streams)/total*100:.1f}%)" if total else "")
    print(f"    HIT:      {len(hits):>6} ({len(hits)/total*100:.1f}%)" if total else "")
    print()

    print(f"  Real billed tokens (what the provider charged for):")
    print(f"    Input:    {billed_prompt:>12,}   @ ${INPUT_PRICE}/M  = ${input_cost:.4f}")
    print(f"    Output:   {billed_completion:>12,}   @ ${OUTPUT_PRICE}/M = ${output_cost:.4f}")
    print(f"    Total:    {billed:>12,}   = ${input_cost+output_cost:.4f}")
    print()

    print(f"  ToolRecall api_cache savings:")
    print(f"    Saved prompt:     {saved_prompt:>10,}   ${saved_prompt/1e6*INPUT_PRICE:.4f}")
    print(f"    Saved completion: {saved_completion:>10,}   ${saved_completion/1e6*OUTPUT_PRICE:.4f}" if saved_completion else "")
    print(f"    Total saved:      ${saved_cost:.4f}")
    saved_pct = saved_cost / (input_cost+output_cost+saved_cost) * 100 if (input_cost+output_cost+saved_cost) > 0 else 0
    print(f"    Saved vs total:   {saved_pct:.2f}%")
    print()

    if provider_cache_read > 0 or provider_cache_write > 0:
        print(f"  Provider prefix-cache (from usage block):")
        print(f"    Cache read tokens:  {provider_cache_read:>10,}")
        print(f"    Cache write tokens: {provider_cache_write:>10,}")
        print(f"    Est. discount:      ${provider_cache_estimated_savings:.4f}")
        print()

    # Breakdown by provider
    hosts = {}
    for r in rows:
        hosts.setdefault(r["host"], {"count": 0, "prompt": 0, "completion": 0, "hits": 0})
        hosts[r["host"]]["count"] += 1
        hosts[r["host"]]["prompt"] += r["prompt"]
        hosts[r["host"]]["completion"] += r["completion"]
        if r["status"] == "HIT":
            hosts[r["host"]]["hits"] += 1

    if len(hosts) > 1:
        print(f"  By provider:")
        for host, data in sorted(hosts.items()):
            cost = (data["prompt"] / 1_000_000 * INPUT_PRICE +
                    data["completion"] / 1_000_000 * OUTPUT_PRICE)
            h = data["hits"]
            print(f"    {host:30s}  {data['count']:4d} reqs  ${cost:.4f}  ({h} hits)")
        print()

    # Average per-request
    non_hit = misses + streams
    if non_hit:
        avg_prompt = billed_prompt // len(non_hit)
        print(f"  Avg per request: {avg_prompt:,} prompt tokens")
        print()

    # ── ToolRecall's total value note ──
    print(f"  ─── Note ───")
    print(f"  These are ONLY api_cache savings from the forward proxy.")
    print(f"  ToolRecall's file_cache and Context Tracker also save")
    print(f"  tokens, but those savings are NOT reflected here.")
    print(f"  See: toolrecall stats  (file_cache + terminal_cache)")
    print()


# ── JSON output ───────────────────────────────────────────────

def report_json(rows: list[dict]) -> dict:
    hits = [r for r in rows if r["status"] == "HIT"]
    misses = [r for r in rows if r["status"] == "MISS"]
    streams = [r for r in rows if r["status"] == "STREAM"]

    billed_prompt = sum(r["prompt"] for r in misses + streams)
    billed_completion = sum(r["completion"] for r in misses + streams)
    saved_prompt = sum(r["prompt"] for r in hits)
    saved_completion = sum(r["completion"] for r in hits)

    return {
        "total_requests": len(rows),
        "hits": len(hits),
        "misses": len(misses),
        "streams": len(streams),
        "billed_prompt_tokens": billed_prompt,
        "billed_completion_tokens": billed_completion,
        "billed_cost": (billed_prompt / 1_000_000 * INPUT_PRICE +
                        billed_completion / 1_000_000 * OUTPUT_PRICE),
        "saved_prompt_tokens": saved_prompt,
        "saved_completion_tokens": saved_completion,
        "saved_cost": (saved_prompt / 1_000_000 * INPUT_PRICE +
                       saved_completion / 1_000_000 * OUTPUT_PRICE),
    }


# ── Tail mode ─────────────────────────────────────────────────

def watch(path: str):
    """Continuously read new lines from CSV and print report every 30s."""
    print(f"Watching {path} for new proxy entries...")
    print("Press Ctrl+C to stop.\n")
    last_size = os.path.getsize(path)
    try:
        while True:
            time.sleep(30)
            current_size = os.path.getsize(path)
            if current_size > last_size:
                rows = load_csv(path)
                os.system("clear")
                report(rows, "Live (last row updated <30s ago)")
            last_size = current_size
    except KeyboardInterrupt:
        print("\nStopped.")


# ── CLI ───────────────────────────────────────────────────────

def main():
    rows = load_csv(CSV_PATH)

    if "--watch" in sys.argv:
        watch(CSV_PATH)
        return

    if "--today" in sys.argv:
        midnight = datetime.datetime.combine(
            datetime.date.today(), datetime.time.min
        ).timestamp()
        rows = filter_since(rows, midnight)

    since_idx = None
    if "--since" in sys.argv:
        try:
            since_idx = sys.argv.index("--since")
            since_str = sys.argv[since_idx + 1]
            since_dt = datetime.datetime.strptime(since_str, "%Y-%m-%d %H:%M:%S")
            rows = filter_since(rows, since_dt.timestamp())
        except (ValueError, IndexError):
            print("Usage: --since 'YYYY-MM-DD HH:MM:SS'")
            sys.exit(1)

    if not rows:
        print("No matching proxy data found.")
        sys.exit(0)

    if "--json" in sys.argv:
        import json
        print(json.dumps(report_json(rows), indent=2))
    else:
        label = ""
        if "--today" in sys.argv:
            label = f"Today ({len(rows)} requests)"
        if since_idx:
            label = f"Since {sys.argv[since_idx+1]} ({len(rows)} requests)"
        report(rows, label)


if __name__ == "__main__":
    main()
