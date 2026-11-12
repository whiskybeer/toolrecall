# Copyright (c) 2026 Robin Schultka
# SPDX-License-Identifier: MIT
# Source: https://github.com/whiskybeer/toolrecall
"""Tests for the Antigravity lifecycle hook adapter (toolrecall.adapters.antigravity_hooks)."""

import os
import time
from pathlib import Path

from toolrecall.adapters import antigravity_hooks


def test_waf_blocks_sensitive_file_read(tmp_path, monkeypatch):
    monkeypatch.setenv("TOOLRECALL_AGY_HOOK_DIR", str(tmp_path / "hooks"))
    event = {
        "hookEventName": "PreToolUse",
        "conversationId": "test-waf-1",
        "toolCall": {
            "name": "read_file",
            "args": {"Path": "/workspace/.env"},
        },
    }
    res = antigravity_hooks.handle(event)
    assert res is not None
    assert res.get("decision") == "deny"
    assert "Blocked access to sensitive file: .env" in res.get("reason", "")


def test_waf_blocks_sensitive_file_edit(tmp_path, monkeypatch):
    monkeypatch.setenv("TOOLRECALL_AGY_HOOK_DIR", str(tmp_path / "hooks"))
    event = {
        "hookEventName": "PreToolUse",
        "conversationId": "test-waf-2",
        "toolCall": {
            "name": "edit_file",
            "args": {"Path": "/project/.env.local"},
        },
    }
    res = antigravity_hooks.handle(event)
    assert res is not None
    assert res.get("decision") == "deny"
    assert "Blocked access to sensitive file: .env.local" in res.get("reason", "")


def test_waf_blocks_dangerous_command(tmp_path, monkeypatch):
    monkeypatch.setenv("TOOLRECALL_AGY_HOOK_DIR", str(tmp_path / "hooks"))
    event = {
        "hookEventName": "PreToolUse",
        "conversationId": "test-waf-3",
        "toolCall": {
            "name": "run_command",
            "args": {"CommandLine": "rm -rf /"},
        },
    }
    res = antigravity_hooks.handle(event)
    assert res is not None
    assert res.get("decision") == "deny"
    assert "Blocked dangerous command pattern" in res.get("reason", "")


def test_waf_allows_safe_tool_call(tmp_path, monkeypatch):
    monkeypatch.setenv("TOOLRECALL_AGY_HOOK_DIR", str(tmp_path / "hooks"))
    event = {
        "hookEventName": "PreToolUse",
        "conversationId": "test-waf-4",
        "toolCall": {
            "name": "read_file",
            "args": {"Path": "/workspace/main.py"},
        },
    }
    res = antigravity_hooks.handle(event)
    assert res == {"decision": "allow"}


def test_waf_can_be_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("TOOLRECALL_AGY_HOOK_DIR", str(tmp_path / "hooks"))
    monkeypatch.setenv("TOOLRECALL_AGY_WAF", "0")
    event = {
        "hookEventName": "PreToolUse",
        "conversationId": "test-waf-5",
        "toolCall": {
            "name": "read_file",
            "args": {"Path": "/workspace/.env"},
        },
    }
    res = antigravity_hooks.handle(event)
    assert res == {"decision": "allow"}


def test_drift_detection_and_pre_invocation_alert(tmp_path, monkeypatch):
    monkeypatch.setenv("TOOLRECALL_AGY_HOOK_DIR", str(tmp_path / "hooks"))
    test_file = tmp_path / "service.py"
    test_file.write_text("def hello(): return 1\n", encoding="utf-8")

    conv_id = "test-drift-conv"

    # 1. PostToolUse on read_file records file state
    read_event = {
        "hookEventName": "PostToolUse",
        "conversationId": conv_id,
        "cwd": str(tmp_path),
        "toolCall": {
            "name": "read_file",
            "args": {"Path": str(test_file)},
        },
    }
    res1 = antigravity_hooks.handle(read_event)
    assert res1 == {}

    # Verify session recorded read
    with antigravity_hooks._session_state(conv_id) as st:
        assert str(test_file.resolve()) in st["reads"]

    # 2. Modify file out-of-band on disk
    test_file.write_text("def hello(): return 2  # modified\n", encoding="utf-8")
    os.utime(test_file, (time.time() + 10, time.time() + 10))

    # 3. PostToolUse on run_command detects out-of-band drift
    cmd_event = {
        "hookEventName": "PostToolUse",
        "conversationId": conv_id,
        "cwd": str(tmp_path),
        "toolCall": {
            "name": "run_command",
            "args": {"CommandLine": "git status"},
        },
    }
    res2 = antigravity_hooks.handle(cmd_event)
    assert res2 == {}

    # Verify stale list updated
    with antigravity_hooks._session_state(conv_id) as st:
        assert str(test_file.resolve()) in st["stale"]

    # 4. PreInvocation injects systemMessage alert
    pre_inv_event = {
        "hookEventName": "PreInvocation",
        "conversationId": conv_id,
    }
    res3 = antigravity_hooks.handle(pre_inv_event)
    assert res3 is not None
    steps = res3.get("injectSteps", [])
    assert len(steps) == 1
    msg = steps[0]["systemMessage"]["systemMessage"]
    assert "TOOLRECALL STALENESS ALERT" in msg
    assert str(test_file.resolve()) in msg

    # 5. Subsequent PreInvocation has no pending alerts
    res4 = antigravity_hooks.handle(pre_inv_event)
    assert res4 == {}


def test_session_start_resets_state(tmp_path, monkeypatch):
    monkeypatch.setenv("TOOLRECALL_AGY_HOOK_DIR", str(tmp_path / "hooks"))
    conv_id = "test-reset-conv"
    with antigravity_hooks._session_state(conv_id) as st:
        st["stale"].append("/some/file.py")

    start_event = {
        "hookEventName": "SessionStart",
        "conversationId": conv_id,
    }
    res = antigravity_hooks.handle(start_event)
    assert res == {}

    # State was deleted/reset
    sfile = Path(tmp_path / "hooks" / f"{conv_id}.json")
    assert not sfile.exists()


def test_hooks_config_schema():
    cfg = antigravity_hooks.hooks_config()
    assert "toolrecall" in cfg
    tr_cfg = cfg["toolrecall"]
    assert "PreToolUse" in tr_cfg
    assert "PostToolUse" in tr_cfg
    assert "PreInvocation" in tr_cfg
    assert "SessionStart" in tr_cfg
    assert tr_cfg["PreToolUse"][0]["matcher"] == "*"
