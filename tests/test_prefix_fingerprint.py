"""Unit tests for P4.1/P4.2 prefix fingerprinting, cache bust detection, and wire stabilization."""

from __future__ import annotations

import concurrent.futures
import json
import os
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

from toolrecall.proxy import (
    _PREFIX_CHAINS,
    _PREFIX_LOCK,
    _USAGE_LOG_HEADER,
    _api_cache_category,
    _compute_prefix_fingerprint,
    _evaluate_prefix_bust,
    _init_usage_log,
    _log_proxy_usage,
    _stabilize_prefix_enabled,
    _stabilize_wire_prefix,
    summarize_prefix_busts,
)


class TestApiCacheCategory(unittest.TestCase):
    def test_category_llm_passthrough_on_dedup_post(self):
        self.assertEqual(_api_cache_category("POST", "/v1/chat/completions"), "llm_passthrough")
        self.assertEqual(_api_cache_category("post", "/v1/messages"), "llm_passthrough")

    def test_category_api_cache_on_non_dedup_or_non_post(self):
        self.assertEqual(_api_cache_category("GET", "/v1/chat/completions"), "api_cache")
        self.assertEqual(_api_cache_category("POST", "/v1/models"), "api_cache")
        self.assertEqual(_api_cache_category("GET", "/v1/models"), "api_cache")


class TestPrefixFingerprint(unittest.TestCase):
    def setUp(self):
        with _PREFIX_LOCK:
            _PREFIX_CHAINS.clear()

    def test_compute_prefix_fingerprint_empty_or_single_turn(self):
        self.assertEqual(_compute_prefix_fingerprint(b""), {})
        self.assertEqual(_compute_prefix_fingerprint(b"not json"), {})
        self.assertEqual(_compute_prefix_fingerprint(b"{}"), {})
        # Single message turn — no prefix turn exists before it
        single = json.dumps({"messages": [{"role": "user", "content": "hello"}]}).encode("utf-8")
        self.assertEqual(_compute_prefix_fingerprint(single), {})

    def test_compute_prefix_fingerprint_openai_messages(self):
        body = json.dumps(
            {
                "system": "You are helpful",
                "messages": [
                    {"role": "user", "content": "Turn 1"},
                    {"role": "assistant", "content": "Reply 1"},
                ],
            }
        ).encode("utf-8")
        fp = _compute_prefix_fingerprint(body, profile="hermes")
        self.assertIn("chain_key", fp)
        self.assertIn("raw_prefix_hash", fp)
        self.assertIn("canon_prefix_hash", fp)
        self.assertIn("prefix_tokens", fp)
        self.assertEqual(len(fp["chain_key"]), 16)
        self.assertEqual(len(fp["raw_prefix_hash"]), 16)
        self.assertEqual(len(fp["canon_prefix_hash"]), 16)
        self.assertGreater(fp["prefix_tokens"], 0)

    def test_compute_prefix_fingerprint_gemini_contents(self):
        body = json.dumps(
            {
                "contents": [
                    {"role": "user", "parts": [{"text": "Gemini 1"}]},
                    {"role": "model", "parts": [{"text": "Gemini 2"}]},
                ]
            }
        ).encode("utf-8")
        fp = _compute_prefix_fingerprint(body)
        self.assertIn("chain_key", fp)
        self.assertIn("raw_prefix_hash", fp)

    def test_same_prefix_same_raw_hash_zero_bust(self):
        body = json.dumps(
            {
                "system": "System instructions",
                "messages": [
                    {"role": "user", "content": "First turn"},
                    {"role": "assistant", "content": "Assistant turn"},
                ],
            }
        ).encode("utf-8")

        raw_hash1, bust1 = _evaluate_prefix_bust(body, profile="hermes")
        self.assertEqual(bust1, 0)
        self.assertTrue(len(raw_hash1) > 0)

        # Repeating exact same prefix
        raw_hash2, bust2 = _evaluate_prefix_bust(body, profile="hermes")
        self.assertEqual(raw_hash1, raw_hash2)
        self.assertEqual(bust2, 0)

    def test_mutated_timestamp_in_system_prompt_causes_cache_bust(self):
        # Turn 1: original timestamp
        payload1 = {
            "system": "Conversation started: 2026-09-01T12:00:00Z\nYou are an agent.",
            "messages": [
                {"role": "user", "content": "Task description"},
                {"role": "assistant", "content": "Doing work"},
            ],
        }
        body1 = json.dumps(payload1).encode("utf-8")
        raw_hash1, bust1 = _evaluate_prefix_bust(body1, profile="hermes")
        self.assertEqual(bust1, 0)

        # Turn 2: same conversation, but system prompt timestamp mutated on wire
        payload2 = {
            "system": "Conversation started: 2026-09-02T15:30:00Z\nYou are an agent.",
            "messages": [
                {"role": "user", "content": "Task description"},
                {"role": "assistant", "content": "Continued work"},
            ],
        }
        body2 = json.dumps(payload2).encode("utf-8")
        raw_hash2, bust2 = _evaluate_prefix_bust(body2, profile="hermes", cache_read_tokens=0)

        # Wire hash changed, but canonical semantic prefix remained the same
        self.assertNotEqual(raw_hash1, raw_hash2)
        self.assertGreater(bust2, 0)

        # If provider reported cache_read_tokens matching prefix_tokens, bust should be 0
        payload3 = {
            "system": "Conversation started: 2026-09-03T18:00:00Z\nYou are an agent.",
            "messages": [
                {"role": "user", "content": "Task description"},
                {"role": "assistant", "content": "Third turn"},
            ],
        }
        body3 = json.dumps(payload3).encode("utf-8")
        _, bust3 = _evaluate_prefix_bust(body3, profile="hermes", cache_read_tokens=10000)
        self.assertEqual(bust3, 0)

    def test_concurrent_interleaved_chains_do_not_bust_each_other(self):
        # Two distinct chains with different user initial queries
        chain_a = json.dumps(
            {
                "system": "Chain A system",
                "messages": [
                    {"role": "user", "content": "Unique Chain A seed"},
                    {"role": "assistant", "content": "A turn 1"},
                ],
            }
        ).encode("utf-8")

        chain_b = json.dumps(
            {
                "system": "Chain B system",
                "messages": [
                    {"role": "user", "content": "Unique Chain B seed"},
                    {"role": "assistant", "content": "B turn 1"},
                ],
            }
        ).encode("utf-8")

        # Interleave calls across threads
        def worker(payload):
            results = []
            for _ in range(10):
                _, bust = _evaluate_prefix_bust(payload, profile="hermes")
                results.append(bust)
            return results

        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            f_a = pool.submit(worker, chain_a)
            f_b = pool.submit(worker, chain_b)
            res_a = f_a.result()
            res_b = f_b.result()

        # Neither chain should register a false-positive bust
        self.assertEqual(sum(res_a), 0)
        self.assertEqual(sum(res_b), 0)


class TestWirePrefixStabilization(unittest.TestCase):
    def test_stabilize_wire_prefix_preserves_last_message(self):
        payload = {
            "system": "Conversation started: 2026-09-01T12:00:00Z\nCurrent time: 2026-09-01 12:00:00",
            "messages": [
                {
                    "role": "user",
                    "content": "<ADDITIONAL_METADATA>\nLocal time: 2026-09-01\n</ADDITIONAL_METADATA>\nHistoric turn 0",
                },
                {
                    "role": "assistant",
                    "content": "Current time: 2026-09-01 12:00:00\nHistoric turn 1",
                },
                {
                    "role": "user",
                    "content": "Current time: 2026-09-01 12:00:00\nLatest turn - MUST NOT BE TOUCHED!",
                },
            ],
        }
        body_bytes = json.dumps(payload).encode("utf-8")
        stabilized_bytes, chars_saved = _stabilize_wire_prefix("hermes", body_bytes)

        self.assertGreater(chars_saved, 0)
        result = json.loads(stabilized_bytes.decode("utf-8"))

        # system and historical turns (messages[:-1]) should be stabilized
        self.assertIn("Conversation started: 1970-01-01T00:00:00Z", result["system"])
        self.assertIn(
            "<ADDITIONAL_METADATA>[STABILIZED]</ADDITIONAL_METADATA>",
            result["messages"][0]["content"],
        )
        self.assertIn("Current time: 1970-01-01T00:00:00Z", result["messages"][1]["content"])

        # Latest turn MUST remain completely unchanged!
        self.assertEqual(
            result["messages"][-1]["content"],
            "Current time: 2026-09-01 12:00:00\nLatest turn - MUST NOT BE TOUCHED!",
        )

    def test_stabilize_wire_prefix_returns_byte_identical_when_clean(self):
        clean_payload = {
            "messages": [
                {"role": "user", "content": "Just standard prompt text"},
                {"role": "assistant", "content": "Standard reply"},
            ]
        }
        body_bytes = json.dumps(clean_payload).encode("utf-8")
        out_bytes, chars_saved = _stabilize_wire_prefix("hermes", body_bytes)
        self.assertEqual(chars_saved, 0)
        self.assertEqual(out_bytes, body_bytes)

    def test_stabilize_prefix_enabled_precedence(self):
        # Header explicit enables
        self.assertTrue(_stabilize_prefix_enabled({"X-ToolRecall-Stabilize-Prefix": "1"}))
        self.assertTrue(_stabilize_prefix_enabled({"X-ToolRecall-Stabilize-Prefix": "true"}))
        # Header explicit disables
        self.assertFalse(_stabilize_prefix_enabled({"X-ToolRecall-Stabilize-Prefix": "0"}))
        self.assertFalse(_stabilize_prefix_enabled({"X-ToolRecall-Stabilize-Prefix": "false"}))

        # Config fallback when header absent
        with patch("toolrecall.config.load_config") as mock_load:
            mock_cfg = MagicMock()
            mock_cfg.get.return_value = True
            mock_load.return_value = mock_cfg
            self.assertTrue(_stabilize_prefix_enabled({}))

            mock_cfg.get.return_value = False
            self.assertFalse(_stabilize_prefix_enabled({}))


class TestUsageLogMigrationAndSummary(unittest.TestCase):
    def test_header_migration_from_v4_to_v5_with_prefix_hash(self):
        old_v4_header = (
            "timestamp,cache_status,target_host,target_path,request_hash,"
            "prompt_tokens,completion_tokens,cache_read_tokens,cache_write_tokens,cost_usd,"
            "dedup_tokens,cache_discount_usd\n"
        )
        sample_row = "1726000000.000,HIT,api.openai.com,/v1/chat/completions,0123456789abcdef,100,50,0,0,0.001000,0,0.000000\n"

        with tempfile.TemporaryDirectory() as tmpdir:
            csv_path = os.path.join(tmpdir, "proxy_usage.csv")
            with open(csv_path, "w") as f:
                f.write(old_v4_header + sample_row)

            with patch("toolrecall.proxy._USAGE_LOG_PATH", csv_path):
                _init_usage_log()

            with open(csv_path, "r") as f:
                lines = f.readlines()

            # First line migrated in place to new header
            self.assertEqual(lines[0], _USAGE_LOG_HEADER)
            self.assertIn("prefix_hash,cache_bust_tokens", lines[0])
            # Data row preserved intact
            self.assertEqual(lines[1], sample_row)

    def test_log_proxy_usage_writes_new_columns(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            csv_path = os.path.join(tmpdir, "proxy_usage.csv")
            with patch("toolrecall.proxy._USAGE_LOG_PATH", csv_path):
                _init_usage_log()
                _log_proxy_usage(
                    "STREAM",
                    "api.openai.com",
                    "/v1/chat/completions",
                    "aabbccddeeff0011",
                    prompt_tokens_override=500,
                    prefix_hash="prefix1234567890",
                    cache_bust_tokens=150,
                )

            with open(csv_path, "r") as f:
                lines = f.readlines()

            self.assertEqual(len(lines), 2)
            row = lines[1].strip().split(",")
            self.assertEqual(row[1], "STREAM")
            self.assertEqual(row[4], "aabbccddeeff0011")
            self.assertEqual(row[12], "prefix1234567890")
            self.assertEqual(row[13], "150")

    def test_summarize_prefix_busts(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            csv_path = os.path.join(tmpdir, "proxy_usage.csv")
            now = time.time()
            rows = [
                _USAGE_LOG_HEADER,
                f"{now - 3600:.3f},STREAM,host,/path,hash,100,50,0,0,0.01,0,0.0,hash1,200\n",
                f"{now - (10 * 86400):.3f},STREAM,host,/path,hash,100,50,0,0,0.01,0,0.0,hash2,100\n",
                f"{now - 1800:.3f},HIT,host,/path,hash,100,50,0,0,0.01,0,0.0,hash3,0\n",
            ]
            with open(csv_path, "w") as f:
                f.writelines(rows)

            summary = summarize_prefix_busts(csv_path, days=7)
            self.assertEqual(summary["total_bust_tokens"], 300)
            self.assertEqual(summary["window_bust_tokens"], 200)
            self.assertEqual(summary["bust_events"], 2)


class TestContextAttributionHook(unittest.TestCase):
    def test_notify_context_attribution_matches_and_sends(self):
        from toolrecall.proxy import _notify_context_attribution

        mock_client = MagicMock()
        mock_client.send.side_effect = lambda cmd: (
            {"anchors": {"/repo/file1.py": {"anchor": "import special_module"}}}
            if cmd.get("cmd") == "context_get_anchors"
            else {"status": "ok"}
        )

        body = b"Turn with import special_module inside content"
        _notify_context_attribution(
            mock_client,
            body,
            prompt_tokens=200,
            cache_read_tokens=50,
            cost_usd=0.002,
        )

        calls = [c[0][0] for c in mock_client.send.call_args_list if isinstance(c[0][0], dict)]
        attr_call = next((c for c in calls if c.get("cmd") == "context_attribute_turn"), None)
        self.assertIsNotNone(attr_call)
        self.assertEqual(attr_call["matched_paths"], ["/repo/file1.py"])
        self.assertEqual(attr_call["prompt_tokens"], 200)
        self.assertEqual(attr_call["cache_read_tokens"], 50)
        self.assertEqual(attr_call["cost_usd"], 0.002)

    def test_notify_context_attribution_ignores_unmatched(self):
        from toolrecall.proxy import _notify_context_attribution

        mock_client = MagicMock()
        mock_client.send.return_value = {"anchors": {"/repo/file1.py": {"anchor": "missing_text"}}}

        body = b"Content without that anchor"
        _notify_context_attribution(
            mock_client,
            body,
            prompt_tokens=200,
            cache_read_tokens=50,
            cost_usd=0.002,
        )

        calls = [c[0][0] for c in mock_client.send.call_args_list if isinstance(c[0][0], dict)]
        attr_call = next((c for c in calls if c.get("cmd") == "context_attribute_turn"), None)
        self.assertIsNone(attr_call)


if __name__ == "__main__":
    unittest.main()
