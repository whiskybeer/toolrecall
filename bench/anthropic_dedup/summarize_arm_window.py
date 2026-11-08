#!/usr/bin/env python3
"""summarize_arm_window.py — per-arm token/cost summary from the proxy usage log.

Purpose
-------
The Claude Code A/B (dedup ON vs OFF) runs through the forward proxy, which
writes one row per request to ``~/.toolrecall/proxy_usage.csv``. This script
turns a TIME WINDOW of that file into the numbers the measurement needs — so
the run itself stays untouched (no instrumentation in the agent, no config
flips mid-run).

CSV columns (header v4):
    timestamp, cache_status, target_host, target_path, request_hash,
    prompt_tokens, completion_tokens, cache_read_tokens, cache_write_tokens,
    cost_usd, dedup_tokens, cache_discount_usd

Token semantics — read this before quoting a number
---------------------------------------------------
Anthropic reports THREE input fields, and they are disjoint:
    input_tokens (uncached, full price) + cache_creation_input_tokens (write,
    ~1.25x) + cache_read_input_tokens (read, ~0.1x) = total prompt tokens.
OpenAI-shaped rows instead report ONE inclusive ``prompt_tokens``. This script
therefore prints BOTH:

    input_uncached   = the full-price input field as logged
    prompt_total     = prompt_tokens + cache_read_tokens + cache_write_tokens

For Anthropic-shaped traffic (Claude Code), **use ``prompt_total``**; for
OpenAI-shaped traffic ``prompt_total`` double-counts nothing (cache fields are
0), so it is safe there too. Never compare ``input_uncached`` across arms and
call it prompt tokens — that is the classic cache-discount mistake.

``cost_usd`` is the PROVIDER-BILLED amount (OpenRouter ``usage.cost``). For
Anthropic it is 0/empty — Anthropic's API reports no cost field. When it is 0
across a window, cost is NOT verifiable from this file: use the provider
console for the run window, and report zero as "unverifiable", never as
"savings".

Usage
-----
    # Per-arm window (run once per arm, right after that arm ended):
    python summarize_arm_window.py --since 2026-09-25T10:00:00 \
        --until 2026-09-25T10:20:00 --label A --out arm_a.json
    python summarize_arm_window.py --since 2026-09-25T10:25:00 \
        --until 2026-09-25T10:45:00 --label B --out arm_b.json

    # Comparison + verdict:
    python summarize_arm_window.py --compare arm_a.json arm_b.json

ARM A = dedup ON (default). ARM B = baseline (header X-ToolRecall-No-Dedup: 1).

What the healthy signature looks like
-------------------------------------
    input_uncached  down     (full-price tokens removed)
    cache_write     down     (1.25x write tokens removed)
    cache_read      ~flat    (the kept earlier copy stays in the cached prefix)
    cache_read share COLLAPSE => the rewrite broke prefix matching: STOP,
                                  that is a bug, not a result.
    dedup_tokens = 0 across the window => the workload had no duplicate
                                  content: "not measurable", never "0%".

Stdlib only — no deps, no network.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from datetime import datetime

DEFAULT_CSV = os.path.expanduser("~/.toolrecall/proxy_usage.csv")

NUM_COLS = (
    "prompt_tokens",
    "completion_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "cost_usd",
    "dedup_tokens",
    "cache_discount_usd",
)


def _num(row: dict, key: str) -> int | float:
    """CSV values are strings; pre-v2 rows lack later columns (DictReader: None)."""
    raw = row.get(key)
    if raw in (None, ""):
        return 0
    try:
        return float(raw)
    except ValueError:
        return 0


def _parse_ts(raw: str | None) -> datetime | None:
    if not raw:
        return None
    txt = raw.strip().replace("Z", "+00:00")
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(txt, fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(txt)
    except ValueError:
        return None


def summarize(
    csv_path: str,
    since: str | None,
    until: str | None,
    label: str,
    host_substr: str = "",
    path_substr: str = "",
) -> dict:
    if not os.path.exists(csv_path):
        print(f"ERROR: usage log not found: {csv_path}", file=sys.stderr)
        return {}

    t_since = _parse_ts(since)
    t_until = _parse_ts(until)

    rows = 0
    aggs = {c: 0.0 for c in NUM_COLS}
    per_host: dict[str, int] = {}
    cache_status: dict[str, int] = {}
    unparsed_ts = 0

    with open(csv_path, newline="", encoding="utf-8", errors="replace") as fh:
        for row in csv.DictReader(fh):
            ts = _parse_ts(row.get("timestamp"))
            if ts is None:
                unparsed_ts += 1
                if t_since or t_until:
                    continue
            else:
                # Compare naively when one side is tz-aware and the other is not:
                # strip tzinfo so a local window still selects rows.
                cmp_ts = ts.replace(tzinfo=None) if ts.tzinfo else ts
                if t_since is not None:
                    lo = t_since.replace(tzinfo=None) if t_since.tzinfo else t_since
                    if cmp_ts < lo:
                        continue
                if t_until is not None:
                    hi = t_until.replace(tzinfo=None) if t_until.tzinfo else t_until
                    if cmp_ts > hi:
                        continue
            # Arm isolation: only rows from the arm's own endpoint count. Other
            # Claude Code sessions in the same time window would otherwise be
            # silently mixed into the arm.
            host = (row.get("target_host") or "").strip()
            if host_substr and host_substr not in host:
                continue
            if path_substr and path_substr not in (row.get("target_path") or ""):
                continue
            rows += 1
            for c in NUM_COLS:
                aggs[c] += _num(row, c)
            host = host or "?"
            per_host[host] = per_host.get(host, 0) + 1
            status = (row.get("cache_status") or "?").strip()
            cache_status[status] = cache_status.get(status, 0) + 1

    prompt_total = aggs["prompt_tokens"] + aggs["cache_read_tokens"] + aggs["cache_write_tokens"]
    cache_read_share = (aggs["cache_read_tokens"] / prompt_total) if prompt_total else 0.0
    dedup_share = (
        aggs["dedup_tokens"] / (prompt_total + aggs["dedup_tokens"])
        if (prompt_total or aggs["dedup_tokens"])
        else 0.0
    )

    return {
        "label": label,
        "csv": csv_path,
        "window": {"since": since, "until": until},
        "filters": {"host": host_substr, "path": path_substr},
        "requests": rows,
        "unparsed_timestamps": unparsed_ts,
        "input_uncached": int(aggs["prompt_tokens"]),
        "prompt_total": int(prompt_total),
        "cache_read": int(aggs["cache_read_tokens"]),
        "cache_write": int(aggs["cache_write_tokens"]),
        "completion": int(aggs["completion_tokens"]),
        "cost_usd": round(aggs["cost_usd"], 6),
        "dedup_tokens": int(aggs["dedup_tokens"]),
        "cache_discount_usd": round(aggs["cache_discount_usd"], 6),
        "cache_read_share": round(cache_read_share, 4),
        "dedup_share_of_sent": round(dedup_share, 4),
        "per_host": per_host,
        "cache_status": cache_status,
    }


def show(s: dict) -> None:
    if not s:
        return
    print(f"── arm {s['label']} " + "─" * 40)
    print(f"  window         {s['window']['since']} → {s['window']['until']}")
    print(f"  requests       {s['requests']}")
    print(f"  input (uncached){s['input_uncached']:>12,}   full price")
    print(f"  cache write    {s['cache_write']:>12,}   ~1.25x")
    print(f"  cache read     {s['cache_read']:>12,}   ~0.1x")
    print(f"  prompt total   {s['prompt_total']:>12,}   input+read+write  ← compare THIS")
    print(f"  completion     {s['completion']:>12,}")
    print(f"  cache-read share {s['cache_read_share']:.1%}")
    print(f"  dedup tokens   {s['dedup_tokens']:>12,}   stubbed, never sent")
    print(f"  billed cost    ${s['cost_usd']:.4f}" + ("   (0 = provider reports none)" if not s["cost_usd"] else ""))
    print(f"  hosts          {s['per_host']}")


def compare(a: dict, b: dict) -> None:
    print("\n══ A (dedup ON) vs B (baseline) " + "═" * 30)

    def pct(a_v: float, b_v: float) -> str:
        if not b_v:
            return "n/a"
        return f"{(a_v - b_v) / b_v * 100:+.1f}%"

    print(f"{'metric':<18}{'A':>14}{'B':>14}{'A vs B':>11}")
    for key, name in (
        ("prompt_total", "prompt total"),
        ("input_uncached", "input uncached"),
        ("cache_write", "cache write"),
        ("cache_read", "cache read"),
        ("completion", "completion"),
    ):
        print(f"{name:<18}{a[key]:>14,}{b[key]:>14,}{pct(a[key], b[key]):>11}")
    print(f"{'cache-read share':<18}{a['cache_read_share']:>13.1%}{b['cache_read_share']:>14.1%}")
    print(f"{'dedup tokens':<18}{a['dedup_tokens']:>14,}{b['dedup_tokens']:>14,}")
    print(f"{'billed cost':<18}{a['cost_usd']:>14.4f}{b['cost_usd']:>14.4f}{pct(a['cost_usd'], b['cost_usd']):>11}")

    print("\nverdict:")
    if a["dedup_tokens"] == 0 and b["dedup_tokens"] == 0:
        print("  dedup never fired in either arm → NO DUPLICATE CONTENT; not measurable.")
        print("  Do not report 0%. Re-run with repeated reads of unchanged files.")
    elif a["cache_read_share"] < b["cache_read_share"] - 0.05:
        print("  cache-read share dropped in arm A → prefix matching BROKE. Stop: bug, not result.")
    else:
        print(f"  prompt-token delta A vs B: {pct(a['prompt_total'], b['prompt_total'])}")
        print("  Cost: quote only if cost_usd is non-zero in both arms; otherwise reconcile")
        print("  against the provider console for both windows and report that as the bill.")


def main() -> int:
    ap = argparse.ArgumentParser(description="Per-arm proxy usage summary (Claude Code dedup A/B)")
    ap.add_argument("--csv", default=DEFAULT_CSV, help=f"usage log (default: {DEFAULT_CSV})")
    ap.add_argument("--since", help="ISO timestamp, inclusive")
    ap.add_argument("--until", help="ISO timestamp, inclusive")
    ap.add_argument("--label", default="?", help="arm label, e.g. A or B")
    ap.add_argument("--out", help="write the summary dict to this JSON file")
    ap.add_argument("--compare", nargs=2, metavar=("A.json", "B.json"), help="compare two summaries")
    ap.add_argument(
        "--host",
        default="api.anthropic.com",
        help="target_host substring filter (default: api.anthropic.com — a bare "
        "'anthropic' would also match test hosts like api.anthropictest.invalid)",
    )
    ap.add_argument(
        "--path",
        default="/v1/messages",
        help="target_path substring filter (default: /v1/messages)",
    )
    ap.add_argument(
        "--no-filter", action="store_true", help="disable both filters (count every row in the window)"
    )
    args = ap.parse_args()

    if args.compare:
        with open(args.compare[0]) as fa, open(args.compare[1]) as fb:
            a, b = json.load(fa), json.load(fb)
        show(a)
        show(b)
        compare(a, b)
        return 0

    s = summarize(
        args.csv,
        args.since,
        args.until,
        args.label,
        host_substr="" if args.no_filter else args.host,
        path_substr="" if args.no_filter else args.path,
    )
    if not s:
        return 1
    show(s)
    if args.out:
        with open(args.out, "w") as fh:
            json.dump(s, fh, indent=2)
        print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
