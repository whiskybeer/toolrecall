# Copyright (c) 2026 Robin Schultka
# SPDX-License-Identifier: MIT
# Source: https://github.com/whiskybeer/toolrecall
"""Tests for the Antigravity transcript audit adapter (toolrecall.adapters.antigravity_audit)."""

import json

from toolrecall.adapters import antigravity_audit


def test_analyse_transcript_basic(tmp_path):
    log_dir = tmp_path / "conv1" / ".system_generated" / "logs"
    log_dir.mkdir(parents=True)
    transcript_file = log_dir / "transcript.jsonl"

    lines = [
        {
            "step_index": 0,
            "type": "USER_INPUT",
            "source": "USER_EXPLICIT",
            "status": "DONE",
            "content": "Analyze performance",
        },
        {
            "step_index": 1,
            "type": "PLANNER_RESPONSE",
            "source": "MODEL",
            "status": "DONE",
            "input_tokens": 5000,
            "cache_read_tokens": 1000,
            "output_tokens": 200,
            "tool_calls": [
                {"name": "read_file", "args": {"Path": "/repo/main.py"}},
                {"name": "read_file", "args": {"Path": "/repo/main.py"}},
                {"name": "run_command", "args": {"CommandLine": "pytest"}},
                {"name": "invoke_subagent", "args": {"Role": "Researcher"}},
                {"name": "swarm", "args": {"action": "add", "n": 2}},
            ],
        },
        {
            "step_index": 2,
            "type": "GENERIC",
            "source": "MODEL",
            "status": "ERROR",
            "content": "Encountered error in tool execution",
        },
    ]

    with open(transcript_file, "w", encoding="utf-8") as f:
        for entry in lines:
            f.write(json.dumps(entry) + "\n")

    res = antigravity_audit.analyse_transcript(str(transcript_file))
    assert res is not None
    assert res["steps"] == 3
    assert res["user_prompts"] == 1
    assert res["model_turns"] == 1
    assert res["input_tokens"] == 5000
    assert res["cache_read_tokens"] == 1000
    assert res["output_tokens"] == 200
    assert res["baseline_tokens"] == 6000
    assert res["tool_calls"]["read_file"] == 2
    assert res["tool_calls"]["run_command"] == 1
    assert res["subagents_spawned"] == 1
    assert res["swarm_workers_spawned"] == 2
    assert "/repo/main.py" in res["redundant_reads"]
    assert res["redundant_reads"]["/repo/main.py"] == 2
    assert res["tool_errors"]["generic_error"] == 1


def test_audit_aggregate_and_report(tmp_path):
    log_dir = tmp_path / "conv2" / ".system_generated" / "logs"
    log_dir.mkdir(parents=True)
    transcript_file = log_dir / "transcript.jsonl"

    lines = [
        {"step_index": 0, "type": "USER_INPUT", "status": "DONE", "content": "Hello"},
        {
            "step_index": 1,
            "type": "PLANNER_RESPONSE",
            "status": "DONE",
            "input_tokens": 10000,
            "output_tokens": 100,
            "tool_calls": [{"name": "read_file", "args": {"Path": "doc.md"}}],
        },
    ]
    with open(transcript_file, "w", encoding="utf-8") as f:
        for entry in lines:
            f.write(json.dumps(entry) + "\n")

    report_data = antigravity_audit.audit(brain_dir=str(tmp_path))
    assert report_data["sessions_analyzed"] == 1
    assert report_data["total_steps"] == 2
    assert report_data["input_tokens_total"] == 10000
    assert report_data["tool_calls"]["read_file"] == 1

    formatted = antigravity_audit.format_report(report_data)
    assert "ToolRecall Audit: Google Antigravity" in formatted
    assert "read_file" in formatted
    assert "drop ~1.5k unused tool schema tokens" in formatted
