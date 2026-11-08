"""Unit tests for normalized deduplication (test/bash output dedup)."""

from __future__ import annotations

import json
import unittest

from toolrecall.dedup import dedup_messages, normalize_output
from toolrecall.proxy import _maybe_dedup_body


class TestNormalizeOutput(unittest.TestCase):
    def test_normalizes_durations(self):
        text = "42 passed in 0.45s\nRan 12 tests took 120ms\nfinished in 1.23 seconds"
        norm = normalize_output(text)
        self.assertIn("in <duration>", norm)
        self.assertIn("took <duration>", norm)
        self.assertNotIn("0.45s", norm)
        self.assertNotIn("120ms", norm)

    def test_normalizes_timestamps(self):
        text = "Log event at 2026-11-07T02:15:30Z completed"
        norm = normalize_output(text)
        self.assertIn("<timestamp>", norm)
        self.assertNotIn("2026-11-07T02:15:30Z", norm)

    def test_normalizes_pytest_tempdirs(self):
        text = "Failure logged at /tmp/pytest-of-runner/pytest-42/test_mod.py line 12"
        norm = normalize_output(text)
        self.assertIn("<pytest-tempdir>", norm)
        self.assertNotIn("pytest-42", norm)

    def test_normalizes_memory_addresses(self):
        text = "<Object at 0x7f9a1b2c3d4e>"
        norm = normalize_output(text)
        self.assertIn("<memaddr>", norm)
        self.assertNotIn("0x7f9a1b2c3d4e", norm)


class TestDedupWithNormalization(unittest.TestCase):
    def setUp(self):
        # A long test failure output (>800 chars) that slightly varies by duration
        base_body = (
            "============================= test session starts ==============================\n"
            "platform linux -- Python 3.11.2, pytest-8.0.0\n"
            "rootdir: /workspace/project\n"
            "collected 50 items\n\n"
            "test_core.py ................................................. [100%]\n"
            "=========================== 1 failed, 49 passed in {duration} ===========================\n"
            "FAILED test_core.py::test_reproduce - AssertionError: assert False\n"
            + ("traceback line in module.py\n" * 40)
        )
        self.output_turn1 = base_body.format(duration="1.25s")
        self.output_turn2 = base_body.format(duration="1.48s")
        self.assertTrue(len(self.output_turn1) > 800)

    def test_raw_dedup_misses_volatile_duration(self):
        msgs = [
            {"role": "user", "content": "run tests"},
            {"role": "tool", "name": "bash", "content": self.output_turn1},
            {"role": "assistant", "content": "I see the failure. Let me run again."},
            {"role": "tool", "name": "bash", "content": self.output_turn2},
        ]
        # Without normalizer: raw strings differ, so not stubbed
        out, stats = dedup_messages(msgs, min_chars=800, protect_last=0)
        self.assertEqual(stats["blocks"], 0)
        self.assertEqual(out[3]["content"], self.output_turn2)

    def test_normalized_dedup_catches_volatile_duration(self):
        msgs = [
            {"role": "user", "content": "run tests"},
            {"role": "tool", "name": "bash", "content": self.output_turn1},
            {"role": "assistant", "content": "I see the failure. Let me run again."},
            {"role": "tool", "name": "bash", "content": self.output_turn2},
        ]
        # With normalizer: normalized text matches, so stubbed!
        out, stats = dedup_messages(
            msgs, min_chars=800, protect_last=0, normalizer=normalize_output
        )
        self.assertEqual(stats["blocks"], 1)
        self.assertIn("[toolrecall-dedup]", out[3]["content"])
        self.assertIn("message 1", out[3]["content"])
        self.assertTrue(stats["chars_saved"] > 700)
        # Not byte-identical, so the stub must not claim it; it keeps this
        # output's own last line verbatim (here the traceback tail).
        self.assertNotIn("byte-identical", out[3]["content"])
        self.assertTrue(out[3]["content"].endswith("traceback line in module.py"))

    def test_normalized_stub_keeps_own_summary_line(self):
        out1 = self.output_turn1 + "=== 1 failed, 49 passed in 1.25s ==="
        out2 = self.output_turn2 + "=== 1 failed, 49 passed in 1.48s ==="
        msgs = [
            {"role": "user", "content": "run tests"},
            {"role": "tool", "name": "bash", "content": out1},
            {"role": "assistant", "content": "again"},
            {"role": "tool", "name": "bash", "content": out2},
        ]
        out, _ = dedup_messages(msgs, min_chars=800, protect_last=0, normalizer=normalize_output)
        self.assertIn(
            "Last line of this output: === 1 failed, 49 passed in 1.48s ===", out[3]["content"]
        )

    def test_anthropic_messages_shape_nested_tool_result(self):
        # Anthropic messages shape with tool_result
        msgs = [
            {"role": "user", "content": "please run tests"},
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "call_1",
                        "content": self.output_turn1,
                    }
                ],
            },
            {"role": "assistant", "content": "retrying"},
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "call_2",
                        "content": self.output_turn2,
                    }
                ],
            },
        ]
        out, stats = dedup_messages(
            msgs, min_chars=800, protect_last=0, normalizer=normalize_output
        )
        self.assertEqual(stats["blocks"], 1)
        part = out[3]["content"][0]
        self.assertEqual(part["type"], "tool_result")
        self.assertEqual(part["tool_use_id"], "call_2")
        self.assertIn("[toolrecall-dedup]", part["content"])


class TestProxyClaudeCodeProfileDedup(unittest.TestCase):
    def test_claude_code_profile_enables_normalization_and_protect_last_zero(self):
        import os

        os.environ["TOOLRECALL_FORWARD_DEDUP"] = "true"
        try:
            base_body = (
                "============================= test session starts ==============================\n"
                "collected 50 items\n\n"
                "=========================== 1 failed in {duration} ===========================\n"
                + ("traceback line in module.py\n" * 40)
            )
            out1 = base_body.format(duration="0.30s")
            out2 = base_body.format(duration="0.55s")
            payload = {
                "messages": [
                    {"role": "user", "content": "run tests"},
                    {"role": "tool", "content": out1},
                    {"role": "tool", "content": out2},
                ]
            }
            body_bytes = json.dumps(payload).encode("utf-8")

            # 1. Without profile: raw dedup misses duration delta
            _, stats_default = _maybe_dedup_body(body_bytes, profile="default")
            self.assertIsNone(stats_default)

            # 2. With profile="claude-code": normalization catches duration delta
            new_bytes, stats_cc = _maybe_dedup_body(body_bytes, profile="claude-code")
            self.assertIsNotNone(stats_cc)
            self.assertEqual(stats_cc["blocks"], 1)
            envelope = json.loads(new_bytes.decode("utf-8"))
            self.assertIn("[toolrecall-dedup]", envelope["messages"][2]["content"])
        finally:
            os.environ.pop("TOOLRECALL_FORWARD_DEDUP", None)


if __name__ == "__main__":
    unittest.main()
