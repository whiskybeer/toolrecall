#!/usr/bin/env python3
# Copyright (c) 2026 Robin Schultka
# SPDX-License-Identifier: MIT
# Source: https://github.com/whiskybeer/toolrecall
"""Discover recurring failure signatures across tool executions.

Read-only analysis tool that discovers recurring error patterns either by
querying the failure ledger in SQLite or parsing a log file, normalizing
errors via toolrecall.failures.normalize_error_signature.

Never writes to any live database (always uses URI mode=ro for SQLite).

Usage:
    python3 scripts/discover_failure_signatures.py [--db PATH] [--min-count N] [--tool TOOL]
    python3 scripts/discover_failure_signatures.py --log PATH [--min-count N] [--tool TOOL]
    python3 scripts/discover_failure_signatures.py --json
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
import re
import sqlite3
import sys
from typing import Any

from toolrecall.failures import normalize_error_signature

DEFAULT_DB_PATH = os.environ.get(
    "TOOLRECALL_CACHE_DB", os.path.expanduser("~/.toolrecall/cache.db")
)


def _format_timestamp(ts: float) -> str:
    """Format Unix timestamp as ISO-like readable UTC string."""
    if not ts:
        return "never"
    try:
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        return dt.strftime("%Y-%m-%d %H:%M:%S UTC")
    except Exception:
        return str(ts)


def discover_from_db(
    db_path: str, min_count: int = 2, tool_filter: str = ""
) -> list[dict[str, Any]]:
    """Query failure_ledger from SQLite DB in read-only mode."""
    if not os.path.exists(db_path):
        return []

    uri = f"file:{os.path.abspath(db_path)}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True)
    except sqlite3.OperationalError as exc:
        print(f"Error opening DB read-only ({db_path}): {exc}", file=sys.stderr)
        return []

    try:
        cur = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='failure_ledger'"
        )
        if not cur.fetchone():
            return []

        query = (
            "SELECT signature_hash, tool, normalized_error, count, first_seen, last_seen, resolution "
            "FROM failure_ledger WHERE count >= ?"
        )
        params: list[Any] = [min_count]
        if tool_filter:
            query += " AND tool = ?"
            params.append(tool_filter)
        query += " ORDER BY count DESC, last_seen DESC"

        rows = conn.execute(query, params).fetchall()
        results: list[dict[str, Any]] = []
        for r in rows:
            results.append(
                {
                    "signature_hash": r[0],
                    "tool": r[1],
                    "normalized_error": r[2],
                    "count": r[3],
                    "first_seen": r[4],
                    "last_seen": r[5],
                    "resolution": r[6],
                }
            )
        return results
    finally:
        conn.close()


def discover_from_log(
    log_path: str, min_count: int = 2, tool_filter: str = ""
) -> list[dict[str, Any]]:
    """Parse log file and group normalized failure signatures."""
    if not os.path.exists(log_path):
        print(f"Log file not found: {log_path}", file=sys.stderr)
        return []

    groups: dict[str, dict[str, Any]] = {}
    line_pattern_1 = re.compile(
        r"\[(?P<tool>[a-zA-Z0-9_\-\.\/]+)\]\s+(?:ERROR|FAILED|Exception):\s*(?P<error>.*)",
        re.I,
    )
    line_pattern_2 = re.compile(
        r"(?P<tool>[a-zA-Z0-9_\-\.\/]+)\s*:\s*(?:ERROR|FAILED|Exception):\s*(?P<error>.*)",
        re.I,
    )

    with open(log_path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line_str = line.strip()
            if not line_str:
                continue

            tool = ""
            error_text = ""

            # Try parsing as JSON first
            if line_str.startswith("{") and line_str.endswith("}"):
                try:
                    data = json.loads(line_str)
                    if isinstance(data, dict):
                        tool = str(data.get("tool") or data.get("tool_name") or "")
                        error_text = str(
                            data.get("error") or data.get("error_text") or data.get("message") or ""
                        )
                except Exception:
                    pass

            if not error_text:
                m1 = line_pattern_1.search(line_str)
                if m1:
                    tool = m1.group("tool")
                    error_text = m1.group("error")
                else:
                    m2 = line_pattern_2.search(line_str)
                    if m2:
                        tool = m2.group("tool")
                        error_text = m2.group("error")
                    elif "error" in line_str.lower() or "exception" in line_str.lower():
                        tool = tool_filter or "tool"
                        error_text = line_str

            if not error_text:
                continue

            if tool_filter and tool != tool_filter:
                continue

            sig_hash, norm_text = normalize_error_signature(tool, error_text)
            if sig_hash not in groups:
                groups[sig_hash] = {
                    "signature_hash": sig_hash,
                    "tool": tool,
                    "normalized_error": norm_text,
                    "count": 0,
                    "first_seen": 0.0,
                    "last_seen": 0.0,
                    "resolution": "",
                }
            groups[sig_hash]["count"] += 1

    filtered = [g for g in groups.values() if g["count"] >= min_count]
    filtered.sort(key=lambda x: (x["count"], x["signature_hash"]), reverse=True)
    return filtered


def print_text_report(signatures: list[dict[str, Any]], min_count: int, source: str) -> None:
    """Print readable text report to stdout."""
    print(f"=== Discovered Recurring Failure Signatures (count >= {min_count}) ===")
    print(f"Source: {source}")
    print(f"Found: {len(signatures)} recurring signature(s)\n")

    if not signatures:
        print("No recurring failures found matching criteria.")
        return

    separator = "-" * 78
    for idx, sig in enumerate(signatures, 1):
        print(separator)
        print(f"[{idx}] Signature:  {sig['signature_hash']}")
        print(f"    Tool:       {sig['tool']}")
        print(f"    Occurrences: {sig['count']}")
        if sig.get("first_seen"):
            print(f"    First seen: {_format_timestamp(sig['first_seen'])}")
            print(f"    Last seen:  {_format_timestamp(sig['last_seen'])}")
        if sig.get("resolution"):
            print(f"    Resolution: {sig['resolution']}")
        print("    Normalized Error:")
        print(f"      {sig['normalized_error']}")

    print(separator)
    total_failures = sum(s["count"] for s in signatures)
    print(f"Total failure instances across recurring signatures: {total_failures}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Discover recurring normalized failure signatures from cache.db or logs."
    )
    parser.add_argument(
        "--db",
        default=DEFAULT_DB_PATH,
        help=f"Path to SQLite database (read-only, default: {DEFAULT_DB_PATH})",
    )
    parser.add_argument(
        "--log",
        default="",
        help="Optional path to log file to parse instead of database",
    )
    parser.add_argument(
        "--min-count",
        type=int,
        default=2,
        help="Minimum occurrence count threshold (default: 2)",
    )
    parser.add_argument(
        "--tool",
        default="",
        help="Filter by specific tool name",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output raw JSON array",
    )

    args = parser.parse_args()

    if args.log:
        source_name = f"Log file ({args.log})"
        results = discover_from_log(args.log, min_count=args.min_count, tool_filter=args.tool)
    else:
        source_name = f"Database ({args.db})"
        results = discover_from_db(args.db, min_count=args.min_count, tool_filter=args.tool)

    if args.json:
        print(json.dumps(results, indent=2))
    else:
        print_text_report(results, min_count=args.min_count, source=source_name)


if __name__ == "__main__":
    main()
