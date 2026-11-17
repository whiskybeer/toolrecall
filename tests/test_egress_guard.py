"""Unit tests for toolrecall/egress.py egress guard."""

from __future__ import annotations

import io
import json
import unittest
from unittest.mock import MagicMock, patch

from toolrecall.egress import EGRESS_RULES, egress_mode_from_config, scan_egress
from toolrecall.proxy import ForwardProxyHandler


class TestEgressGuardPatterns(unittest.TestCase):
    """Verify egress rule patterns match real credential shapes and reject benign strings."""

    def setUp(self):
        # ALWAYS build secret fixtures via concatenation so gitleaks never triggers
        self.ant_key = "sk-ant-" + "api03-" + ("A" * 25)
        self.ant_admin = "sk-ant-" + "admin01-" + ("Z" * 25)
        self.oai_key = "sk-" + "proj-" + ("B" * 25)
        self.oai_svc = "sk-" + "svcacct-" + ("C" * 25)
        self.openrouter_key = "sk-or-" + "v1-" + ("a" * 32)
        self.gh_token = "ghp_" + ("x" * 36)
        self.gh_pat = "github_pat_" + ("y" * 40)
        self.aws_key = "AKIA" + "0123456789ABCDEF"
        self.aws_asia = "ASIA" + "9876543210FEDCBA"
        self.priv_key = "-----BEGIN " + "OPENSSH PRIVATE KEY-----"
        self.priv_rsa = "-----BEGIN " + "RSA PRIVATE KEY-----"
        self.slack_tok = "xoxb-" + ("1234567890" * 2)

    def test_all_patterns_match_intended_credentials(self):
        for pattern_name, text in [
            ("anthropic_api_key", self.ant_key),
            ("anthropic_api_key", self.ant_admin),
            ("openai_api_key", self.oai_key),
            ("openai_api_key", self.oai_svc),
            ("openrouter_api_key", self.openrouter_key),
            ("github_token", self.gh_token),
            ("github_token", self.gh_pat),
            ("aws_access_key", self.aws_key),
            ("aws_access_key", self.aws_asia),
            ("private_key_pem", self.priv_key),
            ("private_key_pem", self.priv_rsa),
            ("slack_token", self.slack_tok),
        ]:
            with self.subTest(rule=pattern_name):
                pattern = EGRESS_RULES[pattern_name]
                matches = pattern.findall(text)
                self.assertTrue(len(matches) >= 1, f"Pattern {pattern_name} failed to match {text}")

    def test_benign_code_and_hashes_do_not_match(self):
        benign_samples = [
            "123e4567-e89b-12d3-a456-426614174000",
            "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
            "c0ffee1234567890abcdef1234567890abcdef12",
            "def test_auth(): return os.environ.get('API_KEY')",
            "https://api.openai.com/v1/chat/completions",
            "Bearer " + ("a" * 32),
            "AKIA_NOT_A_KEY",
            "sk-short",
            "xox-invalid",
        ]
        for sample in benign_samples:
            for rule_name, pattern in EGRESS_RULES.items():
                with self.subTest(rule=rule_name, sample=sample):
                    matches = pattern.findall(sample)
                    self.assertEqual(
                        matches,
                        [],
                        f"Rule {rule_name} had false positive on benign string: {sample}",
                    )


class TestScanEgress(unittest.TestCase):
    """Test policy modes: off, warn, redact, block, and allow rules."""

    def setUp(self):
        self.secret1 = "sk-ant-" + "api03-" + ("A" * 25)
        self.secret2 = "AKIA" + "0123456789ABCDEF"
        self.payload_str = f"Prompt with {self.secret1} and AWS {self.secret2}."
        self.payload_bytes = self.payload_str.encode("utf-8")

    def test_mode_off_returns_allowed_and_original_payload(self):
        res = scan_egress(self.payload_str, mode="off")
        self.assertTrue(res["allowed"])
        self.assertEqual(res["mode"], "off")
        self.assertEqual(res["matches"], [])
        self.assertEqual(res["redacted_payload"], self.payload_str)

    def test_unknown_mode_falls_back_to_off(self):
        res = scan_egress(self.payload_str, mode="unknown_mode")
        self.assertTrue(res["allowed"])
        self.assertEqual(res["mode"], "off")
        self.assertEqual(res["matches"], [])
        self.assertEqual(res["redacted_payload"], self.payload_str)

    def test_empty_payload_returns_allowed(self):
        for empty in ("", b""):
            res = scan_egress(empty, mode="block")
            self.assertTrue(res["allowed"])
            self.assertEqual(res["matches"], [])
            self.assertEqual(res["redacted_payload"], empty)

    def test_mode_warn_reports_matches_but_allows_original_payload(self):
        res = scan_egress(self.payload_str, mode="warn")
        self.assertTrue(res["allowed"])
        self.assertEqual(res["mode"], "warn")
        rules = {m["rule"] for m in res["matches"]}
        self.assertEqual(rules, {"anthropic_api_key", "aws_access_key"})
        # NEVER include secret text in matches
        for m in res["matches"]:
            self.assertIn("rule", m)
            self.assertIn("count", m)
            self.assertEqual(len(m), 2)
        self.assertEqual(res["redacted_payload"], self.payload_str)

    def test_mode_redact_string(self):
        res = scan_egress(self.payload_str, mode="redact")
        self.assertTrue(res["allowed"])
        self.assertEqual(res["mode"], "redact")
        redacted = res["redacted_payload"]
        self.assertIsInstance(redacted, str)
        self.assertNotIn(self.secret1, redacted)
        self.assertNotIn(self.secret2, redacted)
        self.assertIn("[TOOLRECALL_REDACTED:anthropic_api_key]", redacted)
        self.assertIn("[TOOLRECALL_REDACTED:aws_access_key]", redacted)

    def test_mode_redact_bytes(self):
        res = scan_egress(self.payload_bytes, mode="redact")
        self.assertTrue(res["allowed"])
        self.assertEqual(res["mode"], "redact")
        redacted = res["redacted_payload"]
        self.assertIsInstance(redacted, bytes)
        self.assertNotIn(self.secret1.encode(), redacted)
        self.assertNotIn(self.secret2.encode(), redacted)
        self.assertIn(b"[TOOLRECALL_REDACTED:anthropic_api_key]", redacted)
        self.assertIn(b"[TOOLRECALL_REDACTED:aws_access_key]", redacted)

    def test_mode_block_denies_matching_payload(self):
        res = scan_egress(self.payload_str, mode="block")
        self.assertFalse(res["allowed"])
        self.assertEqual(res["mode"], "block")
        self.assertEqual(len(res["matches"]), 2)
        self.assertEqual(res["redacted_payload"], self.payload_str)

    def test_mode_block_allows_clean_payload(self):
        clean = "Normal prompt without any credentials."
        res = scan_egress(clean, mode="block")
        self.assertTrue(res["allowed"])
        self.assertEqual(res["matches"], [])
        self.assertEqual(res["redacted_payload"], clean)

    def test_allow_rules_bypasses_specified_rules(self):
        res = scan_egress(
            self.payload_str,
            mode="block",
            allow_rules=["anthropic_api_key"],
        )
        self.assertFalse(res["allowed"])
        self.assertEqual(len(res["matches"]), 1)
        self.assertEqual(res["matches"][0]["rule"], "aws_access_key")

        # Allow both
        res_both = scan_egress(
            self.payload_str,
            mode="block",
            allow_rules={"anthropic_api_key", "aws_access_key"},
        )
        self.assertTrue(res_both["allowed"])
        self.assertEqual(res_both["matches"], [])


class TestEgressModeFromConfig(unittest.TestCase):
    """Test reading egress configuration cleanly without raising."""

    def test_egress_mode_defaults_when_empty_or_missing(self):
        with patch("toolrecall.config.load_config") as mock_load:
            mock_cfg = MagicMock()
            mock_cfg.get.return_value = None
            mock_load.return_value = mock_cfg

            mode, allow_rules = egress_mode_from_config()
            self.assertEqual(mode, "off")
            self.assertEqual(allow_rules, set())

    def test_egress_mode_reads_configured_values(self):
        with patch("toolrecall.config.load_config") as mock_load:
            mock_cfg = MagicMock()

            def fake_get(section, key, default=None):
                if section == "security" and key == "egress_mode":
                    return "block"
                if section == "security" and key == "egress_allow_rules":
                    return ["slack_token", "github_token"]
                return default

            mock_cfg.get.side_effect = fake_get
            mock_load.return_value = mock_cfg

            mode, allow_rules = egress_mode_from_config()
            self.assertEqual(mode, "block")
            self.assertEqual(allow_rules, {"slack_token", "github_token"})

    def test_egress_mode_never_raises_on_exception(self):
        with patch("toolrecall.config.load_config", side_effect=RuntimeError("disk err")):
            mode, allow_rules = egress_mode_from_config()
            self.assertEqual(mode, "off")
            self.assertEqual(allow_rules, set())


class _MockClient:
    def __init__(self):
        self.sent = []

    def send(self, cmd):
        self.sent.append(cmd)
        return {"cached": False}


class _DummyHandler(ForwardProxyHandler):
    def __init__(self, headers, body_bytes, client):
        self.headers = headers
        self.rfile = io.BytesIO(body_bytes)
        self.wfile = io.BytesIO()
        self.path = "/v1/chat/completions"
        self._client = client
        self.status = None
        self.sent_headers = {}

    def send_response(self, code, message=None):
        self.status = code

    def send_header(self, keyword, value):
        self.sent_headers[keyword] = value

    def end_headers(self):
        pass


class TestProxyEgressIntegration(unittest.TestCase):
    def setUp(self):
        self.secret = "sk-ant-" + "api03-" + ("A" * 25)

    def test_proxy_handle_blocks_and_notifies_daemon_on_block_mode(self):
        client = _MockClient()
        body = json.dumps({"messages": [{"role": "user", "content": self.secret}]}).encode()
        headers = {"Host": "api.anthropic.com", "Content-Length": str(len(body))}
        handler = _DummyHandler(headers, body, client)

        with patch("toolrecall.egress.egress_mode_from_config", return_value=("block", set())):
            handler._handle("POST")

        self.assertEqual(handler.status, 400)
        err = json.loads(handler.wfile.getvalue().decode())
        self.assertEqual(err["error"], "Blocked by ToolRecall egress guard")
        self.assertEqual(err["rules"], ["anthropic_api_key"])

        # Daemon was notified of egress_event
        egress_events = [c for c in client.sent if c.get("cmd") == "egress_event"]
        self.assertEqual(len(egress_events), 1)
        self.assertTrue(egress_events[0]["blocked"])
        self.assertEqual(egress_events[0]["rules"], ["anthropic_api_key"])


if __name__ == "__main__":
    unittest.main()
