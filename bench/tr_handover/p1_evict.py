#!/usr/bin/env python3
"""P1 — eviction ceiling probe on a recorded request body. Free, no API.

W3 asks whether rewriting superseded file bodies in the carried context can
lower the bill. Two numbers decide it before any code is written:

  1. duplicate-byte mass — how much of the outgoing prompt is a body we have
     already sent (all but the most recent copy of each digest);
  2. cached-prefix share — how much of what we send is billed at the cached
     rate. Rewriting a cached prefix forces the suffix to be re-billed at the
     fresh rate, so a high share can make eviction COST MORE.

Pre-registered rule (plan §4, H4): eviction helps only when duplicate bytes are
>40% of the prompt AND the cached-prefix share is <50%. Above that cached
share the expected outcome is a cost increase — a valid finding, not a failure.

Data sources (read-only):
  ~/.hermes/state.db   — the actual messages a session sends (active rows only)
  ~/.toolrecall/proxy_usage.csv — billed prompt/cache_read tokens in the same
                        time window (the billed truth, not an estimate)

Usage:
    python3 bench/tr_handover/p1_evict.py [--session ID] [--min-bytes 2048] [-v]
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import os
import sqlite3

STATE_DB = os.path.expanduser("~/.hermes/state.db")
USAGE_CSV = os.path.expanduser("~/.toolrecall/proxy_usage.csv")


def pick_session(conn) -> tuple[str, int]:
    row = conn.execute(
        """
        SELECT session_id, SUM(LENGTH(content)) AS b
        FROM messages WHERE active = 1
        GROUP BY session_id ORDER BY b DESC LIMIT 1
        """
    ).fetchone()
    if not row:
        raise SystemExit("no active messages in state.db")
    return row[0], row[1]


def load_messages(conn, session_id: str) -> list[dict]:
    cur = conn.execute(
        """
        SELECT id, role, content, tool_name, token_count
        FROM messages WHERE session_id = ? AND active = 1 ORDER BY id
        """,
        (session_id,),
    )
    return [
        {"id": r[0], "role": r[1], "content": r[2] or "", "tool": r[3] or "", "tokens": r[4]}
        for r in cur
    ]


def cached_share(start_ts: float, end_ts: float) -> tuple[float, int, int]:
    """Billed cached-prefix share in a time window, from the proxy CSV."""
    if not os.path.exists(USAGE_CSV):
        return float("nan"), 0, 0
    prompt = cached = 0
    with open(USAGE_CSV, newline="") as f:
        for row in csv.DictReader(f):
            try:
                ts = float(row.get("timestamp") or 0)
                pt = float(row.get("prompt_tokens") or 0)
                cr = float(row.get("cache_read_tokens") or 0)
            except (TypeError, ValueError):
                continue
            if pt <= 0 or not (start_ts <= ts <= end_ts):
                continue
            prompt += int(pt)
            cached += int(cr)
    if not prompt:
        return float("nan"), 0, 0
    return 100.0 * cached / prompt, int(prompt), int(cached)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--session", default="")
    ap.add_argument("--min-bytes", type=int, default=2048)
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    conn = sqlite3.connect(f"file:{STATE_DB}?mode=ro", uri=True)
    session_id, _bytes = (args.session, 0) if args.session else pick_session(conn)
    meta = conn.execute(
        "SELECT started_at, ended_at, input_tokens, cache_read_tokens, model, cwd "
        "FROM sessions WHERE id = ?",
        (session_id,),
    ).fetchone()
    messages = load_messages(conn, session_id)
    if not messages:
        raise SystemExit(f"session {session_id}: no active messages")

    total_chars = sum(len(m["content"]) for m in messages)
    # Eviction targets carried file bodies, i.e. tool results above the floor.
    candidates = [
        (idx, m)
        for idx, m in enumerate(messages)
        if (m["tool"] or m["role"] == "tool") and len(m["content"]) >= args.min_bytes
    ]

    digests: dict[str, dict] = {}
    for idx, m in candidates:
        body = m["content"]
        d = hashlib.sha256(body.encode("utf-8", "ignore")).hexdigest()
        rec = digests.setdefault(
            d, {"digest": d, "bytes": len(body), "idxs": [], "tool": m["tool"], "count": 0}
        )
        rec["idxs"].append(idx)
        rec["count"] += 1

    dup = [r for r in digests.values() if r["count"] > 1]
    evictable = sum(r["bytes"] * (r["count"] - 1) for r in dup)
    dup_pct = 100.0 * evictable / total_chars if total_chars else 0.0

    # "How far back": distance between the first and last copy, in messages.
    spans = [r["idxs"][-1] - r["idxs"][0] for r in dup]

    start = meta[0] or 0
    end = meta[1] or 9e18
    share, prompt_tok, cached_tok = cached_share(start, end)
    # sessions.input_tokens is the UNCACHED input; the prompt is input+cached.
    billed_share = (
        100.0 * (meta[3] / (meta[2] + meta[3])) if meta and (meta[2] + meta[3]) else float("nan")
    )

    print(f"session: {session_id}  model={meta[4] if meta else '?'}  cwd={meta[5] if meta else '?'}")
    print(f"active messages: {len(messages)}  total carried chars: {total_chars:,}")
    print(f"tool-body candidates (>= {args.min_bytes} B): {len(candidates)}  unique digests: {len(digests)}")
    print(f"duplicated digests: {len(dup)}  duplicate bytes (keep-last): {evictable:,}  "
          f"= {dup_pct:.1f}% of the carried prompt")

    if dup:
        print("\ntop duplicated bodies (keep-last evictable):")
        print(f"  {'digest':12} {'count':>5} {'bytes':>8} {'evictable':>10} {'first':>6} {'last':>6} {'span':>5}  tool")
        for r in sorted(dup, key=lambda x: -x["bytes"] * (x["count"] - 1))[:10]:
            print(
                f"  {r['digest'][:12]:12} "
                f"{r['count']:>5} {r['bytes']:>8,} {r['bytes'] * (r['count'] - 1):>10,} "
                f"{r['idxs'][0]:>6} {r['idxs'][-1]:>6} {r['idxs'][-1] - r['idxs'][0]:>5}  {r['tool'] or r['idxs']}"
            )
        spans_sorted = sorted(spans)
        print(
            f"\nspan (messages between first and last copy): "
            f"median {spans_sorted[len(spans_sorted)//2]}, max {spans_sorted[-1]}"
        )

    print("\ncached-prefix share (billed):")
    print(f"  session row: {billed_share:.1f}%  (cached {meta[3]:,} / prompt {meta[2] + meta[3]:,})")
    print(f"  proxy CSV in session window: {share:.1f}%  ({cached_tok:,} / {prompt_tok:,} prompt tokens)")

    share_for_rule = billed_share if billed_share == billed_share else share
    helps = dup_pct > 40.0 and share_for_rule < 50.0
    print("\nH4 (pre-registered): dup > 40% AND cached share < 50% ->")
    print(
        f"  dup={dup_pct:.1f}%, cached={share_for_rule:.1f}% -> "
        f"{'eviction MAY lower cost' if helps else 'EXPECT COST INCREASE or no win'}"
    )
    if not helps:
        print("  (a negative result here is the answer to P1, not a reason to skip it)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
