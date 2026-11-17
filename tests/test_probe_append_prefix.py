"""Tests for scripts/probe_append_prefix.py.

Verifies:
1. longest_common_prefix_len behavior on exact, disjoint, empty, and chunk-spanning bytes.
2. analyze_append_stability classifications:
   - pure_append (direct growth, prefix equals prev)
   - json_array_append (trailing closing brackets/whitespace moved down)
   - rewritten (low common prefix ratio < 0.7)
   - partial_append (common prefix ratio >= 0.7 but not pure/json array append)
3. CLI snapshot and compare functionality producing valid Markdown reports.
"""

from __future__ import annotations

import subprocess
import sys

from scripts.probe_append_prefix import (
    analyze_append_stability,
    longest_common_prefix_len,
)


def test_longest_common_prefix_len():
    # Empty cases
    assert longest_common_prefix_len(b"", b"") == 0
    assert longest_common_prefix_len(b"", b"abc") == 0
    assert longest_common_prefix_len(b"abc", b"") == 0

    # Disjoint
    assert longest_common_prefix_len(b"abc", b"def") == 0

    # Partial match
    assert longest_common_prefix_len(b"abcdef", b"abcxyz") == 3

    # Identical
    assert longest_common_prefix_len(b"toolrecall", b"toolrecall") == 10

    # Cross 4096-byte chunk boundary
    prefix_large = b"X" * 8192
    assert longest_common_prefix_len(prefix_large + b"ABC", prefix_large + b"ABD") == 8194


def test_analyze_pure_append():
    prev = b"Initial log content.\n"
    curr = b"Initial log content.\nSecond line added.\n"
    res = analyze_append_stability(prev, curr)

    assert res["classification"] == "pure_append"
    assert res["prev_bytes"] == len(prev)
    assert res["curr_bytes"] == len(curr)
    assert res["growth_bytes"] == len(curr) - len(prev)
    assert res["common_prefix_bytes"] == len(prev)
    assert res["stable_prefix_ratio"] == 1.0


def test_analyze_json_array_append():
    prev = b'[\n  {"id": 1, "task": "init"}\n]\n'
    curr = b'[\n  {"id": 1, "task": "init"},\n  {"id": 2, "task": "step"}\n]\n'
    res = analyze_append_stability(prev, curr)

    assert res["classification"] == "json_array_append"
    assert res["growth_bytes"] > 0
    assert res["common_prefix_bytes"] < len(prev)
    assert (len(prev) - res["common_prefix_bytes"]) <= 64
    assert res["stable_prefix_ratio"] > 0.8


def test_analyze_rewritten_file():
    prev = b"The quick brown fox jumps over the lazy dog." * 5
    curr = b"Completely different rewritten text entirely." * 5
    res = analyze_append_stability(prev, curr)

    assert res["classification"] == "rewritten"
    assert res["stable_prefix_ratio"] < 0.7


def test_analyze_partial_append():
    # Stable prefix ratio >= 0.7, but diff > 64 bytes with non-JSON content
    base = b"A" * 750
    prev = base + (b"B" * 250)
    curr = base + (b"C" * 250)
    res = analyze_append_stability(prev, curr)

    assert res["classification"] == "partial_append"
    assert res["stable_prefix_ratio"] == 0.75
    assert res["common_prefix_bytes"] == 750


def test_cli_snapshot_and_compare(tmp_path):
    target_file = tmp_path / "data.json"
    target_file.write_text('[\n  {"item": 1}\n]\n', encoding="utf-8")

    snapshot_dir = tmp_path / "snapshots"
    report_file = tmp_path / "report.md"

    python_exe = sys.executable

    # 1. Snapshot
    cmd_snap = [
        python_exe,
        "scripts/probe_append_prefix.py",
        "--snapshot",
        "--paths",
        str(target_file),
        "--snapshot-dir",
        str(snapshot_dir),
    ]
    res_snap = subprocess.run(cmd_snap, capture_output=True, text=True, check=True)
    assert "Snapshotted" in res_snap.stdout
    assert (snapshot_dir / "manifest.json").is_file()

    # 2. Modify target (JSON append)
    target_file.write_text('[\n  {"item": 1},\n  {"item": 2}\n]\n', encoding="utf-8")

    # 3. Compare
    cmd_comp = [
        python_exe,
        "scripts/probe_append_prefix.py",
        "--compare",
        "--snapshot-dir",
        str(snapshot_dir),
        "--report",
        str(report_file),
    ]
    res_comp = subprocess.run(cmd_comp, capture_output=True, text=True, check=True)
    assert "Wrote report" in res_comp.stdout
    assert report_file.is_file()

    content = report_file.read_text(encoding="utf-8")
    assert "# Prefix Stability & Append Analysis Report" in content
    assert "json_array_append" in content
