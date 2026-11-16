"""Unit tests for bench/crossover/ (Tier 1 replay, instrumentation, tasks, analysis, 5 gates)."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

BENCH_DIR = Path(__file__).resolve().parents[1] / "bench" / "crossover"
if str(BENCH_DIR) not in sys.path:
    sys.path.insert(0, str(BENCH_DIR))

import analyze_crossover as ac  # noqa: E402
import instrument as inst  # noqa: E402
import tasks as tmod  # noqa: E402
import tier1_replay as t1  # noqa: E402


def test_tier1_replay_blocks_computes_repeat_share_and_savings():
    b1 = b"A" * 1000
    b2 = b"B" * 1200
    dg1 = hashlib.sha256(b1).digest()
    dg2 = hashlib.sha256(b2).digest()
    # 5 blocks: b1, b2, b1, b1, b1 -> repeated b1s outside protect_last=2 get stubbed
    blocks = [(dg1, len(b1)), (dg2, len(b2)), (dg1, len(b1)), (dg1, len(b1)), (dg1, len(b1))]
    res = t1.replay_blocks(blocks, protect_last=2)
    assert res["n_blocks"] == 5
    assert res["distinct_block_bytes"] == 2200
    assert res["repeated_block_bytes"] == 3000
    assert abs(res["repeat_share"] - (3000 / 2200)) < 1e-9
    assert res["stub_bytes_served"] > 0
    assert res["pct_saved"] > 0


def test_instrument_extract_session_turns_and_proxy_crossref(tmp_path: Path):
    big_a = "X" * 1000
    big_b = "Y" * 1000
    jsonl = tmp_path / "sess.jsonl"
    entries = [
        {"type": "user", "message": {"content": [{"type": "tool_result", "content": big_a}]}},
        {
            "type": "assistant",
            "message": {
                "id": "m1",
                "content": [{"type": "tool_use", "name": "Read", "input": {"file_path": "a.py"}}],
                "usage": {
                    "input_tokens": 100,
                    "cache_read_input_tokens": 0,
                    "cache_creation_input_tokens": 500,
                    "output_tokens": 20,
                },
            },
        },
        {"type": "user", "message": {"content": [{"type": "tool_result", "content": big_b}]}},
        {
            "type": "assistant",
            "message": {
                "id": "m2",
                "content": [{"type": "tool_use", "name": "Read", "input": {"file_path": "a.py"}}],
                "usage": {
                    "input_tokens": 100,
                    "cache_read_input_tokens": 500,
                    "cache_creation_input_tokens": 200,
                    "output_tokens": 25,
                },
            },
        },
        {"type": "user", "message": {"content": [{"type": "tool_result", "content": big_a}]}},
        {
            "type": "assistant",
            "message": {
                "id": "m3",
                "content": [],
                "usage": {
                    "input_tokens": 100,
                    "cache_read_input_tokens": 700,
                    "cache_creation_input_tokens": 100,
                    "output_tokens": 30,
                },
            },
        },
    ]
    jsonl.write_text("\n".join(json.dumps(e) for e in entries) + "\n")

    turns_a = inst.extract_session_turns(jsonl, arm="A")
    assert len(turns_a) == 3
    assert turns_a[0]["prompt_tokens"] == 600
    assert turns_a[0]["stub_bytes_served"] == 0
    assert turns_a[2]["distinct_block_bytes"] == 2000
    assert turns_a[2]["repeated_block_bytes"] == 1000
    assert abs(turns_a[2]["repeat_share"] - 0.5) < 1e-6
    assert turns_a[2]["unique_files_read"] == 1
    assert turns_a[2]["total_file_reads"] == 2

    turns_b = inst.extract_session_turns(jsonl, arm="B", proxy_dedup_tokens=[0, 0, 220])
    assert turns_b[2]["stub_bytes_served"] == 880


def test_analyze_crossover_decomposition_crossover_and_gates(tmp_path: Path):
    runs_path = tmp_path / "runs.jsonl"
    rows = []

    # 1. Null arm pair (Gate 1): identical tokens -> net_usd == 0 -> CI contains 0
    for rep, order in ((1, "AB"), (2, "BA")):
        for arm in ("A", "B"):
            rows.append(
                {
                    "task": "null_qa",
                    "repo": "null",
                    "workload": "null",
                    "arm": arm,
                    "arm_order": order,
                    "rep": rep,
                    "resolved": True,
                    "turns": 1,
                    "input_tokens": 50,
                    "cache_read": 0,
                    "cache_creation": 0,
                    "output_tokens": 20,
                    "repeat_share": 0.0,
                    "stub_bytes_served": 0,
                }
            )

    # 2. Long workload pairs (3 reps, counterbalanced): Arm B pays cache-creation early, wins later
    for rep, order in ((1, "AB"), (2, "BA"), (3, "AB")):
        # Per-turn files for crossover detection:
        # Turn 1: B pays extra cache_creation (+10,000 tokens -> -$0.0375)
        # Turn 2: B saves cache_read (+50,000 -> +$0.015) -> cum -$0.0225
        # Turn 3: B saves cache_read (+100,000 -> +$0.030) -> cum +$0.0075 (CROSSOVER at turn 3!)
        ta = [
            {
                "turn": 1,
                "input_tokens": 100,
                "cache_read": 0,
                "cache_creation": 10000,
                "output_tokens": 50,
            },
            {
                "turn": 2,
                "input_tokens": 100,
                "cache_read": 100000,
                "cache_creation": 0,
                "output_tokens": 50,
            },
            {
                "turn": 3,
                "input_tokens": 100,
                "cache_read": 150000,
                "cache_creation": 0,
                "output_tokens": 50,
            },
        ]
        tb = [
            {
                "turn": 1,
                "input_tokens": 100,
                "cache_read": 0,
                "cache_creation": 20000,
                "output_tokens": 50,
            },
            {
                "turn": 2,
                "input_tokens": 100,
                "cache_read": 50000,
                "cache_creation": 0,
                "output_tokens": 50,
            },
            {
                "turn": 3,
                "input_tokens": 100,
                "cache_read": 50000,
                "cache_creation": 0,
                "output_tokens": 50,
            },
        ]
        (tmp_path / f"django__dj_long__long__A_r{rep}.turns.jsonl").write_text(
            "\n".join(json.dumps(x) for x in ta) + "\n"
        )
        (tmp_path / f"django__dj_long__long__B_r{rep}.turns.jsonl").write_text(
            "\n".join(json.dumps(x) for x in tb) + "\n"
        )

        rows.append(
            {
                "task": "dj_long",
                "repo": "django",
                "workload": "long",
                "arm": "A",
                "arm_order": order,
                "rep": rep,
                "resolved": True,
                "turns": 160,
                "input_tokens": 300,
                "cache_read": 250000,
                "cache_creation": 10000,
                "output_tokens": 150,
                "repeat_share": 0.60,
                "stub_bytes_served": 0,
            }
        )
        rows.append(
            {
                "task": "dj_long",
                "repo": "django",
                "workload": "long",
                "arm": "B",
                "arm_order": order,
                "rep": rep,
                "resolved": True,
                "turns": 160,
                "input_tokens": 300,
                "cache_read": 100000,
                "cache_creation": 20000,
                "output_tokens": 150,
                "repeat_share": 0.60,
                "stub_bytes_served": 600000,
            }
        )

    runs_path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    res = ac.analyze(tmp_path)
    assert res["crossover_turn"] == 3
    for gname, ginfo in res["gates"].items():
        assert ginfo["status"] == "PASS", f"{gname} failed: {ginfo}"


def test_build_tasks_covers_2x2_plus_null():
    tasks = tmod.build_tasks("all", "all")
    cells = {(t.repo, t.workload) for t in tasks}
    assert ("null", "null") in cells
    assert ("django", "short") in cells
    assert ("django", "long") in cells
    assert ("toolrecall", "short") in cells
    assert ("toolrecall", "long") in cells
