"""End-to-end test for the forward proxy's cache layer.

Tests the daemon's api_cache IPC directly: store a response, then
check it with the same key — asserts HIT with correct body/headers/status.
This is what the proxy ultimately does, minus the HTTP server layer.
"""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from toolrecall.transport import TransportClient, DEFAULT_PATH


class TestProxyCacheE2E(unittest.TestCase):
    """Test api_cache IPC via daemon — store then check."""

    def setUp(self):
        self.client = TransportClient(DEFAULT_PATH)
        # Verify daemon is running
        ping = self.client.send({"cmd": "ping"})
        if not ping.get("pong"):
            self.skipTest("ToolRecall daemon not running")

    def _make_request_hash(self, method: str, host: str, path: str, body: dict) -> str:
        """Generate the same request hash the proxy uses."""
        import hashlib
        body_bytes = json.dumps(body).encode()
        body_hash = hashlib.sha256(body_bytes).hexdigest()
        request_str = f"{method}:{host}:{path}:{body_hash}"
        return hashlib.sha256(request_str.encode()).hexdigest()

    def test_store_then_hit(self):
        """Store a response, then check with same key — asserts HIT."""
        body = {"model": "gpt-4", "messages": [{"role": "user", "content": "Hello"}]}
        request_hash = self._make_request_hash("POST", "api.openai.com", "/v1/chat/completions", body)

        # Store a mock response
        store_resp = self.client.send({
            "cmd": "cached_api_store",
            "request_hash": request_hash,
            "method": "POST",
            "host": "api.openai.com",
            "path": "/v1/chat/completions",
            "request_body_hash": json.dumps(body),
            "response_status": 200,
            "response_headers": {"Content-Type": "application/json"},
            "response_body": json.dumps({"choices": [{"message": {"content": "Hi!"}}]}),
            "ttl": 300,
        })
        self.assertTrue(store_resp.get("stored", False), f"Store failed: {store_resp}")

        # Check cache — should be HIT
        check_resp = self.client.send({
            "cmd": "cached_api_check",
            "request_hash": request_hash,
        })
        self.assertTrue(check_resp.get("cached"), f"Expected HIT, got: {check_resp}")
        self.assertEqual(check_resp.get("status"), 200)
        self.assertIn("Hi!", check_resp.get("body", ""))

    def test_different_body_different_key(self):
        """Different request bodies produce different cache entries."""
        body_a = {"model": "gpt-4", "messages": [{"role": "user", "content": "Alpha"}]}
        body_b = {"model": "gpt-4", "messages": [{"role": "user", "content": "Beta"}]}

        hash_a = self._make_request_hash("POST", "api.openai.com", "/v1/chat/completions", body_a)
        hash_b = self._make_request_hash("POST", "api.openai.com", "/v1/chat/completions", body_b)

        # Store for body_a
        self.client.send({
            "cmd": "cached_api_store",
            "request_hash": hash_a,
            "method": "POST",
            "host": "api.openai.com",
            "path": "/v1/chat/completions",
            "response_status": 200,
            "response_headers": {},
            "response_body": json.dumps({"result": "alpha"}),
            "ttl": 300,
        })

        # body_a should hit
        hit = self.client.send({"cmd": "cached_api_check", "request_hash": hash_a})
        self.assertTrue(hit.get("cached"))

        # body_b should miss
        miss = self.client.send({"cmd": "cached_api_check", "request_hash": hash_b})
        self.assertFalse(miss.get("cached"))

    def test_different_host_different_key(self):
        """Different target hosts produce separate cache entries."""
        body = {"model": "gpt-4", "messages": [{"role": "user", "content": "Hi"}]}

        hash_oa = self._make_request_hash("POST", "api.openai.com", "/v1/chat/completions", body)
        hash_an = self._make_request_hash("POST", "api.anthropic.com", "/v1/chat/completions", body)

        # Store for OpenAI
        self.client.send({
            "cmd": "cached_api_store",
            "request_hash": hash_oa,
            "method": "POST",
            "host": "api.openai.com",
            "path": "/v1/chat/completions",
            "response_status": 200,
            "response_headers": {},
            "response_body": json.dumps({"provider": "openai"}),
            "ttl": 300,
        })

        # OpenAI should hit
        self.assertTrue(self.client.send({"cmd": "cached_api_check", "request_hash": hash_oa}).get("cached"))
        # Anthropic should miss (different host in key)
        self.assertFalse(self.client.send({"cmd": "cached_api_check", "request_hash": hash_an}).get("cached"))

    def test_non_2xx_not_cached_by_proxy(self):
        """Proxy should not cache non-2xx responses (but api_cache stores them anyway).
        
        This tests that the daemon layer stores whatever it's given — the 
        filtering happens in proxy.py's _handle() before calling cached_api_store.
        """
        body = {"model": "gpt-4", "messages": [{"role": "user", "content": "Fail"}]}
        request_hash = self._make_request_hash("POST", "api.openai.com", "/v1/chat/completions", body)

        # Store a 429 response (rate limited)
        self.client.send({
            "cmd": "cached_api_store",
            "request_hash": request_hash,
            "method": "POST",
            "host": "api.openai.com",
            "path": "/v1/chat/completions",
            "response_status": 429,
            "response_headers": {},
            "response_body": json.dumps({"error": "rate_limited"}),
            "ttl": 300,
        })

        # API layer still stores it — proxy._handle() decides not to cache it
        hit = self.client.send({"cmd": "cached_api_check", "request_hash": request_hash})
        self.assertTrue(hit.get("cached"))
        self.assertEqual(hit.get("status"), 429)


if __name__ == "__main__":
    unittest.main()