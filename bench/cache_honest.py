#!/usr/bin/env python3
"""Honest cache health report — reads the daemon's SQLite DB directly.

The daemon's ``toolrecall status`` counts every in-memory LRU hit as "tokens saved",
which massively inflates the numbers (10K+ "hits" for 1,128 files that each hit disk
exactly once).  This script ignores the in-memory counter and reports only what the
SQLite layer saw — real re-reads of persisted content.

Usage:
    /tmp/bench-env/bin/python3 bench/cache_honest.py
"""

import sqlite3
import os
import time

DB = os.path.expanduser("~/.toolrecall/cache.db")


def _fmt_ts(ts: float) -> str:
    return time.strftime("%H:%M:%S", time.localtime(ts))


def main() -> None:
    con = sqlite3.connect(DB)

    # ── file_cache: actual SQLite-persisted entries ──────────
    rows = con.execute(
        "SELECT path, hits, size FROM file_cache ORDER BY hits DESC"
    ).fetchall()

    total_entries = len(rows)
    once_only = sum(1 for r in rows if r[1] == 1)
    re_read = sum(1 for r in rows if r[1] > 1)
    total_hits_sqlite = sum(r[1] for r in rows)  # count of disk-reads + re-reads
    total_size_chars = sum(r[2] for r in rows)

    # ── cache_stats: the daemon's cumulative counter ─────────
    stats = con.execute(
        "SELECT hits, misses, tokens_read_from_disk, tokens_saved, context_tokens_saved, updated_at "
        "FROM cache_stats WHERE category='file_cache'"
    ).fetchone()

    # Real SQLite-level saved tokens = sum of (hits_per_file × estimated_tokens)
    # where estimated_tokens = size // 4 (chars/token rule from cache.py)
    tok_saved_real = sum(r[1] * (r[2] // 4) for r in rows)

    # Tokens actually read from disk = what the daemon recorded on first reads
    tok_read_from_disk = stats[2] if stats else 0
    tok_claimed_by_daemon = stats[3] if stats else 0

    # ── Output ─────────────────────────────────────────────
    print("=" * 62)
    print("  HONEST CACHE REPORT")
    print("=" * 62)

    print(f"\n  File Cache               SQLite layer only")
    print(f"  ─────────────────────────────────────────────")
    print(f"  Unique files cached:       {total_entries:>8,}")
    print(f"  Read from disk (once):     {once_only:>8,}")
    print(f"  Re-read from SQLite:       {re_read:>8,}")
    if total_entries:
        print(f"  Re-read fraction:          {re_read / total_entries * 100:>7.1f}%")
    print(f"  Total SQLite hits:         {total_hits_sqlite:>8,}")
    print(f"  Total content stored:      {total_size_chars:>10,} chars")

    if stats:
        hr = stats[0] / (stats[0] + stats[1]) * 100 if (stats[0] + stats[1]) else 0
        print(f"\n  Daemon counter (in-memory + SQLite combined)")
        print(f"  ─────────────────────────────────────────────")
        print(f"  Hits:                     {stats[0]:>8,}")
        print(f"  Misses:                   {stats[1]:>8,}")
        print(f"  Hit rate:                 {hr:>6.1f}%")
        if stats[5]:
            print(f"  Last updated:             {_fmt_ts(stats[5])}")

    print(f"\n  Token Accounting          (chars/4 estimate)")
    print(f"  ─────────────────────────────────────────────")
    print(f"  Tokens read from disk:     {tok_read_from_disk:>12,}  ← real I/O cost")
    print(f"  Tokens saved (SQLite):     {tok_saved_real:>12,}  ← actual re-reads avoided")
    print(f"  Daemon claims saved:       {tok_claimed_by_daemon:>12,}  ← in-memory hits included")
    if tok_claimed_by_daemon:
        print(f"  Real fraction of claim:    {tok_saved_real / tok_claimed_by_daemon * 100:>6.1f}%")

    # ── Turn log ────────────────────────────────────────────
    turn_count = con.execute("SELECT COUNT(*) FROM turn_log").fetchone()[0]
    if turn_count:
        # Last run summary
        last_run = con.execute(
            "SELECT arm, workload_id, MAX(turn_index), MAX(ts) "
            "FROM turn_log GROUP BY run_id ORDER BY MAX(ts) DESC LIMIT 3"
        ).fetchall()
        print(f"\n  Recent Benchmark Runs")
        print(f"  ─────────────────────────────────────────────")
        print(f"  Turn log entries total:   {turn_count:>8,}")
        for r in last_run:
            print(f"  {r[0]:12s}  {r[1]:12s}  {r[2]:>4d} turns  last @ {_fmt_ts(r[3])}")

    con.close()

    print()
    print("─" * 62)
    print("  BOTTOM LINE")
    print()
    print("  The daemon's status command counts EVERY in-memory LRU cache hit")
    print(f"  as tokens_saved — same {total_entries} files served from RAM 10× each.")
    print(f"  Real content ever read from disk: {tok_read_from_disk:,} tokens.")
    print()
    print("  For benchmark comparisons, ignore cache_stats entirely.")
    print("  Use turn_log.request_tokens — that's what was actually sent to")
    print("  the LLM API. That number is the honest comparison between arms.")


if __name__ == "__main__":
    main()