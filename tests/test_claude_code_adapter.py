"""Unit tests for toolrecall.adapters.claude_code."""

from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from toolrecall.adapters import claude_code as cc


class TestClaudeCodeAdapter(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.hook_dir = Path(self.tmpdir.name) / "hooks"
        os.environ["TOOLRECALL_CC_HOOK_DIR"] = str(self.hook_dir)
        os.environ.pop("TOOLRECALL_CC_WAF", None)
        self.sid = "test-session-1"
        self.file1 = Path(self.tmpdir.name) / "service.py"
        self.file1.write_text("def run():\n    return 42\n", encoding="utf-8")

    def tearDown(self):
        self.tmpdir.cleanup()
        os.environ.pop("TOOLRECALL_CC_HOOK_DIR", None)
        os.environ.pop("TOOLRECALL_CC_WAF", None)

    def test_post_tool_use_read_records_file(self):
        event = {
            "hook_event_name": "PostToolUse",
            "tool_name": "Read",
            "tool_input": {"file_path": str(self.file1)},
            "session_id": self.sid,
            "cwd": self.tmpdir.name,
        }
        res = cc.handle(event)
        self.assertIsNone(res)

        state_file = self.hook_dir / f"{self.sid}.json"
        self.assertTrue(state_file.exists())
        state = json.loads(state_file.read_text(encoding="utf-8"))
        self.assertIn(str(self.file1.resolve()), state["reads"])
        self.assertEqual(state["reads"][str(self.file1.resolve())]["size"], len(self.file1.read_bytes()))

    def test_no_staleness_when_file_unchanged(self):
        # 1. Read
        cc.handle({
            "hook_event_name": "PostToolUse",
            "tool_name": "Read",
            "tool_input": {"file_path": str(self.file1)},
            "session_id": self.sid,
            "cwd": self.tmpdir.name,
        })
        # 2. Bash tool runs (e.g. running tests)
        res = cc.handle({
            "hook_event_name": "PostToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "pytest"},
            "session_id": self.sid,
            "cwd": self.tmpdir.name,
        })
        self.assertIsNone(res)

    def test_staleness_alert_emitted_when_file_modified(self):
        # 1. Read
        cc.handle({
            "hook_event_name": "PostToolUse",
            "tool_name": "Read",
            "tool_input": {"file_path": str(self.file1)},
            "session_id": self.sid,
            "cwd": self.tmpdir.name,
        })

        # 2. External modification occurs (e.g. git checkout or build step)
        time.sleep(0.05)
        self.file1.write_text("def run():\n    return 100 # updated\n", encoding="utf-8")

        # 3. Next modifying tool finishes
        res = cc.handle({
            "hook_event_name": "PostToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "git status"},
            "session_id": self.sid,
            "cwd": self.tmpdir.name,
        })
        self.assertIsNotNone(res)
        self.assertIn("hookSpecificOutput", res)
        self.assertEqual(res["hookSpecificOutput"]["hookEventName"], "PostToolUse")
        ctx = res["hookSpecificOutput"]["additionalContext"]
        self.assertIn("TOOLRECALL STALENESS ALERT", ctx)
        self.assertIn(str(self.file1.name), ctx)

        # 4. Repeated Bash tool does not repeat the alert for the same modification
        res2 = cc.handle({
            "hook_event_name": "PostToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "git diff"},
            "session_id": self.sid,
            "cwd": self.tmpdir.name,
        })
        self.assertIsNone(res2)

        # 5. Agent re-reads the fresh file
        cc.handle({
            "hook_event_name": "PostToolUse",
            "tool_name": "Read",
            "tool_input": {"file_path": str(self.file1)},
            "session_id": self.sid,
            "cwd": self.tmpdir.name,
        })

        # 6. Subsequent tool execution reports clean
        res3 = cc.handle({
            "hook_event_name": "PostToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "pytest"},
            "session_id": self.sid,
            "cwd": self.tmpdir.name,
        })
        self.assertIsNone(res3)

    def _ev(self, tool, tool_input):
        return cc.handle({
            "hook_event_name": "PostToolUse",
            "tool_name": tool,
            "tool_input": tool_input,
            "session_id": self.sid,
            "cwd": self.tmpdir.name,
        })

    def test_agent_own_edits_never_alert(self):
        # Regression: every agent Edit to a previously read file used to raise a
        # false "re-read" alert, adding a turn and tokens to ordinary sessions.
        self._ev("Read", {"file_path": str(self.file1)})
        for k in range(3):
            time.sleep(0.02)
            self.file1.write_text(f"def run():\n    return {k}\n", encoding="utf-8")
            self.assertIsNone(self._ev("Edit", {"file_path": str(self.file1)}))
            self.assertIsNone(self._ev("Bash", {"command": "pytest -q"}))

    def test_drift_after_agent_edit_still_alerts(self):
        self._ev("Read", {"file_path": str(self.file1)})
        time.sleep(0.02)
        self.file1.write_text("def run():\n    return 1\n", encoding="utf-8")
        self.assertIsNone(self._ev("Write", {"file_path": str(self.file1)}))
        time.sleep(0.02)
        self.file1.write_text("def run():\n    return 2  # codegen\n", encoding="utf-8")
        res = self._ev("Bash", {"command": "make codegen"})
        self.assertIsNotNone(res)
        self.assertIn(self.file1.name, res["hookSpecificOutput"]["additionalContext"])

    def test_daemon_stale_list_is_opt_in(self):
        from unittest import mock

        self._ev("Read", {"file_path": str(self.file1)})
        with mock.patch.object(cc, "_check_daemon_stale", return_value=["/elsewhere/other_agent.py"]):
            os.environ.pop("TOOLRECALL_CC_DAEMON_STALE", None)
            self.assertIsNone(self._ev("Bash", {"command": "ls"}))
            os.environ["TOOLRECALL_CC_DAEMON_STALE"] = "1"
            try:
                res = self._ev("Bash", {"command": "ls"})
            finally:
                os.environ.pop("TOOLRECALL_CC_DAEMON_STALE", None)
        self.assertIn("other_agent.py", res["hookSpecificOutput"]["additionalContext"])

    def test_session_lifecycle_resets_tracking(self):
        # Read file
        cc.handle({
            "hook_event_name": "PostToolUse",
            "tool_name": "Read",
            "tool_input": {"file_path": str(self.file1)},
            "session_id": self.sid,
            "cwd": self.tmpdir.name,
        })
        state_file = self.hook_dir / f"{self.sid}.json"
        self.assertTrue(state_file.exists())

        # PreCompact event resets session
        cc.handle({
            "hook_event_name": "PreCompact",
            "session_id": self.sid,
        })
        self.assertFalse(state_file.exists())

    def test_waf_blocking_pre_tool_use(self):
        os.environ["TOOLRECALL_CC_WAF"] = "1"

        # 1. Block access to .env
        env_file = Path(self.tmpdir.name) / ".env"
        res_env = cc.handle({
            "hook_event_name": "PreToolUse",
            "tool_name": "Read",
            "tool_input": {"file_path": str(env_file)},
            "session_id": self.sid,
        })
        self.assertIsNotNone(res_env)
        self.assertEqual(res_env["hookSpecificOutput"]["hookEventName"], "PreToolUse")
        self.assertEqual(res_env["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIn("sensitive file", res_env["hookSpecificOutput"]["permissionDecisionReason"])

        # 2. Block dangerous rm -rf /
        res_cmd = cc.handle({
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "rm -rf /"},
            "session_id": self.sid,
        })
        self.assertIsNotNone(res_cmd)
        self.assertEqual(res_cmd["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIn("dangerous command", res_cmd["hookSpecificOutput"]["permissionDecisionReason"])

        # 3. Allow safe command
        res_safe = cc.handle({
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "pytest -q"},
            "session_id": self.sid,
        })
        self.assertIsNone(res_safe)

    def test_print_config(self):
        cfg = cc.hooks_config("my-hook-cmd")
        self.assertIn("hooks", cfg)
        self.assertIn("PostToolUse", cfg["hooks"])
        self.assertEqual(cfg["hooks"]["PostToolUse"][0]["hooks"][0]["command"], "my-hook-cmd")


if __name__ == "__main__":
    unittest.main()
