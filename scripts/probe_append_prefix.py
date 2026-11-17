#!/usr/bin/env python3
"""Prefix stability and append classification probe for ToolRecall.

Analyzes file growth and common prefix retention across snapshots to classify
updates as pure appends, JSON array appends, partial appends, or full rewrites.
Zero runtime dependencies (stdlib only).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone


def longest_common_prefix_len(prev: bytes, curr: bytes) -> int:
    """Return the length in bytes of the longest common prefix of prev and curr."""
    limit = min(len(prev), len(curr))
    i = 0
    step = 4096
    while i + step <= limit:
        if prev[i : i + step] == curr[i : i + step]:
            i += step
        else:
            break
    while i < limit and prev[i] == curr[i]:
        i += 1
    return i


def analyze_append_stability(prev: bytes, curr: bytes) -> dict:
    """Analyze stability and classify the delta between prev and curr byte contents.

    Returns:
        {
            "prev_bytes": int,
            "curr_bytes": int,
            "growth_bytes": int,
            "common_prefix_bytes": int,
            "stable_prefix_ratio": float,
            "classification": "pure_append" | "json_array_append" | "rewritten" | "partial_append",
        }
    """
    prev_bytes = len(prev)
    curr_bytes = len(curr)
    growth_bytes = curr_bytes - prev_bytes
    common_prefix = longest_common_prefix_len(prev, curr)
    stable_prefix_ratio = (float(common_prefix) / prev_bytes) if prev_bytes > 0 else 1.0

    # Classification rules:
    # 1. pure_append: common_prefix_bytes == len(prev) and len(curr) >= len(prev)
    # 2. json_array_append: len(prev) > 0 and (len(prev) - common_prefix_bytes <= 64)
    #    and prev[common_prefix_bytes:].strip() consists only of JSON closing tokens ], }, ,, whitespace
    # 3. rewritten: stable_prefix_ratio < 0.7
    # 4. partial_append: otherwise
    _json_closing_set = set(b"]},\r\n\t ")
    if common_prefix == prev_bytes and curr_bytes >= prev_bytes:
        classification = "pure_append"
    elif (
        prev_bytes > 0
        and (prev_bytes - common_prefix <= 64)
        and len(prev[common_prefix:].strip()) > 0
        and set(prev[common_prefix:].strip()).issubset(_json_closing_set)
    ):
        classification = "json_array_append"
    elif stable_prefix_ratio < 0.7:
        classification = "rewritten"
    else:
        classification = "partial_append"

    return {
        "prev_bytes": prev_bytes,
        "curr_bytes": curr_bytes,
        "growth_bytes": growth_bytes,
        "common_prefix_bytes": common_prefix,
        "stable_prefix_ratio": stable_prefix_ratio,
        "classification": classification,
    }


def _snapshot(paths: list[str], snapshot_dir: str) -> None:
    """Snapshot target paths into snapshot_dir with manifest metadata."""
    os.makedirs(snapshot_dir, exist_ok=True)
    manifest_path = os.path.join(snapshot_dir, "manifest.json")
    manifest = {}
    if os.path.isfile(manifest_path):
        try:
            with open(manifest_path, "r", encoding="utf-8") as f:
                manifest = json.load(f)
        except Exception:
            manifest = {}

    import hashlib

    count = 0
    for path in paths:
        abs_path = os.path.abspath(os.path.expanduser(path))
        if not os.path.isfile(abs_path):
            print(f"Warning: file not found: {abs_path}", file=sys.stderr)
            continue
        try:
            with open(abs_path, "rb") as f:
                content = f.read()
        except OSError as e:
            print(f"Error reading {abs_path}: {e}", file=sys.stderr)
            continue

        h = hashlib.sha256(abs_path.encode("utf-8")).hexdigest()[:16]
        base = os.path.basename(abs_path)
        snap_filename = f"{h}_{base}.snap"
        snap_path = os.path.join(snapshot_dir, snap_filename)
        with open(snap_path, "wb") as f:
            f.write(content)

        manifest[abs_path] = {
            "snapshot_file": snap_filename,
            "bytes": len(content),
            "timestamp": time.time(),
        }
        count += 1
        print(f"Snapshotted {abs_path} ({len(content)} bytes) -> {snap_filename}")

    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)
    print(f"Saved manifest for {count} file(s) in {snapshot_dir}")


def _compare(snapshot_dir: str, report_path: str, filter_paths: list[str] | None = None) -> None:
    """Compare current files with snapshots and write a markdown report."""
    manifest_path = os.path.join(snapshot_dir, "manifest.json")
    if not os.path.isfile(manifest_path):
        print(f"Error: manifest not found at {manifest_path}", file=sys.stderr)
        sys.exit(1)

    with open(manifest_path, "r", encoding="utf-8") as f:
        manifest = json.load(f)

    results = []
    for abs_path, meta in sorted(manifest.items()):
        if filter_paths:
            resolved_filters = [os.path.abspath(os.path.expanduser(p)) for p in filter_paths]
            if abs_path not in resolved_filters:
                continue

        snap_file = os.path.join(snapshot_dir, meta["snapshot_file"])
        if not os.path.isfile(snap_file):
            print(f"Warning: snapshot file missing: {snap_file}", file=sys.stderr)
            continue
        with open(snap_file, "rb") as f:
            prev_bytes = f.read()

        if not os.path.isfile(abs_path):
            print(f"Warning: current target missing: {abs_path}", file=sys.stderr)
            continue
        with open(abs_path, "rb") as f:
            curr_bytes = f.read()

        analysis = analyze_append_stability(prev_bytes, curr_bytes)
        analysis["path"] = abs_path
        results.append(analysis)

    # Generate Markdown report
    os.makedirs(os.path.dirname(os.path.abspath(report_path)), exist_ok=True)
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    lines = [
        "# Prefix Stability & Append Analysis Report",
        "",
        f"- **Generated At**: {now_iso}",
        f"- **Snapshot Directory**: `{snapshot_dir}`",
        f"- **Files Compared**: {len(results)}",
        "",
        "| File | Prev Bytes | Curr Bytes | Growth | Common Prefix | Ratio | Classification |",
        "|---|---:|---:|---:|---:|---:|---|",
    ]

    for r in results:
        ratio_str = f"{r['stable_prefix_ratio'] * 100:.1f}%"
        lines.append(
            f"| `{r['path']}` | {r['prev_bytes']} | {r['curr_bytes']} | "
            f"{r['growth_bytes']} | {r['common_prefix_bytes']} | "
            f"{ratio_str} | **{r['classification']}** |"
        )

    # Summary
    class_counts: dict[str, int] = {}
    for r in results:
        class_counts[r["classification"]] = class_counts.get(r["classification"], 0) + 1

    lines.extend(
        [
            "",
            "## Summary",
            "",
            f"- **Pure Append**: {class_counts.get('pure_append', 0)}",
            f"- **JSON Array Append**: {class_counts.get('json_array_append', 0)}",
            f"- **Partial Append**: {class_counts.get('partial_append', 0)}",
            f"- **Rewritten**: {class_counts.get('rewritten', 0)}",
            "",
        ]
    )

    report_content = "\n".join(lines) + "\n"
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(report_content)

    print(f"Wrote report for {len(results)} file(s) to {report_path}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Probe append stability and prefix retention on file updates."
    )
    parser.add_argument(
        "--snapshot",
        action="store_true",
        help="Capture snapshots of specified --paths.",
    )
    parser.add_argument(
        "--compare",
        action="store_true",
        help="Compare current target files against recorded snapshots.",
    )
    parser.add_argument(
        "--paths",
        nargs="+",
        default=[],
        help="Target file paths to snapshot or compare.",
    )
    parser.add_argument(
        "--snapshot-dir",
        default="bench/probe/snapshots",
        help="Directory to store snapshot files and manifest (default: bench/probe/snapshots).",
    )
    parser.add_argument(
        "--report",
        default="bench/probe/REPORT.md",
        help="Path for generated comparison Markdown report (default: bench/probe/REPORT.md).",
    )

    args = parser.parse_args()

    if not args.snapshot and not args.compare:
        parser.print_help()
        return 1

    if args.snapshot:
        if not args.paths:
            print("Error: --snapshot requires --paths", file=sys.stderr)
            return 1
        _snapshot(args.paths, args.snapshot_dir)

    if args.compare:
        _compare(args.snapshot_dir, args.report, filter_paths=args.paths if args.paths else None)

    return 0


if __name__ == "__main__":
    sys.exit(main())
