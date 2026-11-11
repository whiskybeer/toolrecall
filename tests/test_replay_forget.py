"""Tests for toolrecall replay forget and run-scoped cache eviction."""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from toolrecall import cache
from toolrecall.proxy import _canonicalize_body
from toolrecall.replay import ReplayManager
from toolrecall.replay_cli import _cmd_forget


class TestReplayForget(unittest.TestCase):
    def setUp(self):
        cache._init()

    def _store_api(self, req_hash: str, body: str = "ok", run_id: str | None = None):
        return cache.cached_api_store(
            request_hash=req_hash,
            method="POST",
            host="api.anthropic.com",
            path="/v1/messages",
            request_body_hash="bhash",
            response_status=200,
            response_headers={"Content-Type": "application/json"},
            response_body=body,
            run_id=run_id,
        )

    def test_forget_by_run_id(self):
        self._store_api("h_fail_1", "res1", run_id="failed-run-123")
        self._store_api("h_fail_2", "res2", run_id="failed-run-123")
        self._store_api("h_keep", "res3", run_id="success-run-456")

        # Verify initial state
        check1 = cache.cached_api_check("h_fail_1")
        check_keep = cache.cached_api_check("h_keep")
        self.assertTrue(check1["cached"])
        self.assertTrue(check_keep["cached"])

        # Forget failed run
        res = cache.forget_run("failed-run-123")
        self.assertEqual(res["target"], "failed-run-123")
        self.assertEqual(res["forgotten"], 2)

        # Verify failed entries evicted, success entry kept
        self.assertFalse(cache.cached_api_check("h_fail_1")["cached"])
        self.assertFalse(cache.cached_api_check("h_fail_2")["cached"])
        self.assertTrue(cache.cached_api_check("h_keep")["cached"])

    def test_forget_by_trace_dir(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            trace_path = os.path.join(tmpdir, "trace.jsonl")
            with open(trace_path, "w", encoding="utf-8") as fh:
                fh.write(json.dumps({"request_hash": "trace_h1", "hit": False}) + "\n")
                fh.write(json.dumps({"request_hash": "trace_h2", "hit": False}) + "\n")

            self._store_api("trace_h1", "data1")
            self._store_api("trace_h2", "data2")
            self._store_api("trace_other", "data3")

            res = cache.forget_run(tmpdir)
            self.assertEqual(res["forgotten"], 2)

            self.assertFalse(cache.cached_api_check("trace_h1")["cached"])
            self.assertFalse(cache.cached_api_check("trace_h2")["cached"])
            self.assertTrue(cache.cached_api_check("trace_other")["cached"])

    def test_forget_scenario(self):
        rm = ReplayManager()
        rm.record_call("test-scen", "cached_read", {"path": "/foo"}, "file content")
        self.assertIn("test-scen", [s["name"] for s in rm.list_scenarios()])

        res = cache.forget_run("test-scen")
        self.assertTrue(res["scenario_deleted"])
        self.assertNotIn("test-scen", [s["name"] for s in rm.list_scenarios()])

    def test_forget_unknown_fails_soft(self):
        res = cache.forget_run("nonexistent-run")
        self.assertEqual(res["target"], "nonexistent-run")
        self.assertEqual(res["forgotten"], 0)
        self.assertFalse(res["scenario_deleted"])

    def test_path_like_target_never_joins_trace_root(self):
        """A target with separators must not be joined into ~/.toolrecall/traces."""
        self.assertTrue(cache._is_plain_trace_name("failed-run-123"))
        self.assertTrue(cache._is_plain_trace_name("scen.v2"))
        for bad in ("..", ".", "", "a/b", "../escape", "a\\b", "/abs/path"):
            self.assertFalse(cache._is_plain_trace_name(bad), bad)

    def test_antigravity_canonicalization_profile(self):
        t1 = (
            "You are Antigravity.\n"
            "<ADDITIONAL_METADATA>\n"
            "The current local time is: 2026-11-08T00:23:56Z.\n"
            "</ADDITIONAL_METADATA>\n"
            "Conversation ID: 858ad02b-f68d-4625-a34c-44c0d83d06bc\n"
            "Artifact Directory Path: /home/user/.gemini/antigravity-cli/brain/858ad02b-f68d-4625-a34c-44c0d83d06bc\n"
            "Ran 5 tests in 0.42s"
        )
        t2 = (
            "You are Antigravity.\n"
            "<ADDITIONAL_METADATA>\n"
            "The current local time is: 2026-11-08T09:45:10Z.\n"
            "</ADDITIONAL_METADATA>\n"
            "Conversation ID: 11223344-5566-7788-9900-aabbccddeeff\n"
            "Artifact Directory Path: /home/user/.gemini/antigravity-cli/brain/11223344-5566-7788-9900-aabbccddeeff\n"
            "Ran 5 tests in 1.89s"
        )
        body1 = json.dumps({"messages": [{"role": "user", "content": t1}]}).encode()
        body2 = json.dumps({"messages": [{"role": "user", "content": t2}]}).encode()

        c1 = _canonicalize_body("antigravity", body1)
        c2 = _canonicalize_body("antigravity", body2)

        self.assertIsNotNone(c1)
        self.assertIsNotNone(c2)
        self.assertEqual(c1, c2)

    def test_cli_replay_forget(self):
        self._store_api("h_cli_forget", "cli_res", run_id="ci-job-99")
        self.assertTrue(cache.cached_api_check("h_cli_forget")["cached"])

        buf = io.StringIO()
        with redirect_stdout(buf):
            _cmd_forget(["ci-job-99"])

        out = buf.getvalue()
        self.assertIn("Forgot 1 cached API response(s) for run 'ci-job-99'", out)
        self.assertFalse(cache.cached_api_check("h_cli_forget")["cached"])

    def test_antigravity_dedup_normalize(self):
        from toolrecall.proxy import _maybe_dedup_body

        with patch.dict(os.environ, {"TOOLRECALL_FORWARD_DEDUP": "1"}):
            block1 = "Repeated large output with duration: passed in 1.25s\n" + ("x" * 900)
            block2 = "Repeated large output with duration: passed in 2.80s\n" + ("x" * 900)
            body = json.dumps(
                {
                    "messages": [
                        {"role": "user", "content": "run 1"},
                        {"role": "tool", "content": block1},
                        {"role": "tool", "content": block2},
                    ]
                }
            ).encode()
            out_bytes, stats = _maybe_dedup_body(body, profile="antigravity")
            self.assertIsNotNone(stats)
            self.assertEqual(stats["blocks"], 1)
            envelope = json.loads(out_bytes.decode())
            self.assertIn("[toolrecall-dedup] Same output", envelope["messages"][2]["content"])


if __name__ == "__main__":
    unittest.main()
