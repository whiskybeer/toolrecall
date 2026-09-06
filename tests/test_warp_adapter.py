# Copyright (c) 2026 Robin Schultka
# SPDX-License-Identifier: MIT
# Source: https://www.github.com/whiskybeer/toolrecall
"""Tests for the Warp adapter (toolrecall.adapters.warp).

Two layers:
1. Unit tests — pure functions (hash, provider normalization, config build).
2. E2E over real sockets — a mock "TR proxy" upstream validates that the edge
   annotates requests with X-Target-Host, relays bodies/headers/statuses
   byte-faithfully, and returns 502 on upstream failure.

No daemon and no real API calls are required.
"""

import hashlib
import http.server
import json
import threading
import unittest

from http.server import BaseHTTPRequestHandler, HTTPServer

from toolrecall.adapters import warp


# ---- Unit tests ------------------------------------------------------


class TestNormalizeProvider(unittest.TestCase):
    def test_bare_host(self):
        self.assertEqual(warp.normalize_provider("api.openai.com"), "api.openai.com")

    def test_url_form(self):
        self.assertEqual(
            warp.normalize_provider("https://api.anthropic.com/v1"),
            "api.anthropic.com",
        )

    def test_case_and_whitespace(self):
        self.assertEqual(warp.normalize_provider("  OpenRouter.AI "), "openrouter.ai")

    def test_rejects_unknown(self):
        with self.assertRaises(ValueError):
            warp.normalize_provider("internal-metadata.example.com")

    def test_rejects_private(self):
        with self.assertRaises(ValueError):
            warp.normalize_provider("169.254.169.254")


class TestMakeRequestHash(unittest.TestCase):
    def test_deterministic(self):
        a = warp.make_request_hash("POST", "api.openai.com", "/v1/chat/completions", b"{}")
        b = warp.make_request_hash("POST", "api.openai.com", "/v1/chat/completions", b"{}")
        self.assertEqual(a, b)

    def test_matches_proxy_derivation(self):
        """Hash must equal the documented proxy key derivation."""
        body = b'{"model":"gpt-4"}'
        body_hash = hashlib.sha256(body).hexdigest()
        expected = hashlib.sha256(
            f"POST:api.openai.com:/v1/chat/completions:{body_hash}".encode()
        ).hexdigest()
        self.assertEqual(
            warp.make_request_hash("POST", "api.openai.com", "/v1/chat/completions", body),
            expected,
        )

    def test_differs_by_body(self):
        a = warp.make_request_hash("POST", "h", "/p", b"a")
        b = warp.make_request_hash("POST", "h", "/p", b"b")
        self.assertNotEqual(a, b)


class TestBuildEdgeConfig(unittest.TestCase):
    def test_valid(self):
        cfg = warp.build_edge_config("https://api.openai.com/", "https://tr.example.com")
        self.assertEqual(cfg["provider"], "api.openai.com")
        self.assertEqual(cfg["public_url"], "https://tr.example.com")
        self.assertEqual(cfg["version"], 1)

    def test_rejects_http_public_url(self):
        with self.assertRaises(ValueError):
            warp.build_edge_config("api.openai.com", "http://tr.example.com")

    def test_rejects_localhost_public_url(self):
        with self.assertRaises(ValueError):
            warp.build_edge_config("api.openai.com", "http://localhost:8569")

    def test_default_ports_recorded(self):
        cfg = warp.build_edge_config("api.deepseek.com", "https://x.example.com")
        self.assertEqual(cfg["proxy_port"], 8569)


# ---- E2E: edge -> mock TR proxy ---------------------------------------


class MockProxyHandler(http.server.BaseHTTPRequestHandler):
    """Stands in for the ToolRecall forward proxy. Records what it received
    and replies with a canned OpenAI-style completion."""

    last_request: dict = {}

    def log_message(self, fmt, *args):
        return

    def _capture(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length)
        MockProxyHandler.last_request = {
            "path": self.path,
            "x_target_host": self.headers.get("X-Target-Host"),
            "host": self.headers.get("Host"),
            "authorization": self.headers.get("Authorization"),
            "body": body,
            "content_type": self.headers.get("Content-Type"),
            # full lower-cased header map for hop-by-hop assertions
            "headers": {k.lower(): v for k, v in self.headers.items()},
        }

    def do_POST(self):
        self._capture()
        resp = json.dumps({"choices": [{"message": {"content": "cached-or-fresh"}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("X-Mock-Proxy", "yes")
        self.end_headers()
        self.wfile.write(resp)


class TestEdgeE2E(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Mock TR proxy on an ephemeral port.
        cls.proxy_srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), MockProxyHandler)
        cls.proxy_port = cls.proxy_srv.server_address[1]
        threading.Thread(target=cls.proxy_srv.serve_forever, daemon=True).start()

        # Edge wired to the mock proxy, also on an ephemeral port.
        warp.WarpEdgeHandler.proxy_port = cls.proxy_port
        cls.edge_srv = warp.ThreadedEdgeServer(("127.0.0.1", 0), warp.WarpEdgeHandler)
        cls.edge_port = cls.edge_srv.server_address[1]
        warp.WarpEdgeHandler.provider_host = "api.openai.com"
        threading.Thread(target=cls.edge_srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.edge_srv.shutdown()
        cls.proxy_srv.shutdown()

    def _post(self, port, body: bytes, host_header: str = "warp-edge.example.com"):
        import http.client

        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        headers = {
            "Host": host_header,  # Warp sends its own Host — the public domain
            "Content-Type": "application/json",
            "Authorization": "Bearer sk-test-123",
        }
        conn.request("POST", "/v1/chat/completions", body=body, headers=headers)
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        return resp, data

    def test_end_to_end_annotation_and_relay(self):
        body = json.dumps(
            {"model": "gpt-4", "messages": [{"role": "user", "content": "hi"}]}
        ).encode()
        resp, data = self._post(self.edge_port, body)

        self.assertEqual(resp.status, 200)
        self.assertIn(b"cached-or-fresh", data)
        self.assertEqual(resp.getheader("X-Mock-Proxy"), "yes")

        seen = MockProxyHandler.last_request
        # The critical contract: the edge annotated the real provider host,
        # and Warp's public-domain Host header was replaced.
        self.assertEqual(seen["x_target_host"], "api.openai.com")
        self.assertEqual(seen["host"], f"127.0.0.1:{self.proxy_port}")
        self.assertEqual(seen["path"], "/v1/chat/completions")
        self.assertEqual(seen["body"], body)  # byte-faithful relay
        self.assertEqual(seen["authorization"], "Bearer sk-test-123")

    def test_byte_faithful_body_preserved(self):
        body = b'{"model":"gpt-4","messages":[],"x":1}'
        _, data = self._post(self.edge_port, body)
        self.assertIn(b"cached-or-fresh", data)
        self.assertEqual(MockProxyHandler.last_request["body"], body)

    def test_hop_by_hop_stripped(self):
        body = b"{}"
        self._post(self.edge_port, body)
        seen = MockProxyHandler.last_request["headers"]
        # transfer-encoding / connection must not be copied upstream.
        self.assertNotIn("transfer-encoding", seen)
        self.assertNotIn("connection", seen)
        # The edge must not leak Warp's public-domain Host upstream — it is
        # replaced with the TR proxy's own host.
        self.assertTrue(seen["host"].startswith("127.0.0.1:"))

    def test_502_when_upstream_down(self):
        import http.client
        import socket

        # Bind a socket, note the port, close it — a guaranteed-refused local
        # port without touching any non-loopback address (conftest guards
        # against production-looking connections).
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            dead_port = s.getsockname()[1]

        warp.WarpEdgeHandler.proxy_port = dead_port
        try:
            conn = http.client.HTTPConnection("127.0.0.1", self.edge_port, timeout=5)
            conn.request("POST", "/v1/chat/completions", body=b"{}")
            resp = conn.getresponse()
            conn.close()
            self.assertEqual(resp.status, 502)
        finally:
            warp.WarpEdgeHandler.proxy_port = self.proxy_port


class TestEdgeAuth(unittest.TestCase):
    """Ingress auth: when auth_token is set, the edge 401s before relaying."""

    def setUp(self):
        import threading

        class MarkerProxyHandler(BaseHTTPRequestHandler):
            last_auth = None

            def log_message(self, fmt, *args):
                return

            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                self.rfile.read(length)
                MarkerProxyHandler.last_auth = self.headers.get("Authorization")
                body = b'{"ok":true}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.marker_cls = MarkerProxyHandler
        self.proxy = HTTPServer(("127.0.0.1", 0), MarkerProxyHandler)
        self.proxy_port = self.proxy.server_address[1]
        threading.Thread(target=self.proxy.serve_forever, daemon=True).start()

    def tearDown(self):
        self.proxy.shutdown()

    def _make_edge(self, token):
        import threading

        warp.WarpEdgeHandler.auth_token = token
        # Point the edge at the marker proxy — NOT the production proxy on
        # 8569 (the class default). Otherwise "valid token" tests hit the
        # real proxy/upstream and 401s come from the wrong place.
        warp.WarpEdgeHandler.proxy_host = "127.0.0.1"
        warp.WarpEdgeHandler.proxy_port = self.proxy_port
        server = warp.ThreadedEdgeServer(("127.0.0.1", 0), warp.WarpEdgeHandler)
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        return port

    def _post(self, port, auth_header=None):
        import http.client

        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        headers = {"Content-Type": "application/json"}
        if auth_header is not None:
            headers["Authorization"] = auth_header
        conn.request("POST", "/v1/chat/completions", body=b"{}", headers=headers)
        resp = conn.getresponse()
        resp.read()
        conn.close()
        return resp

    def test_no_token_configured_allows_all(self):
        port = self._make_edge(None)
        self.assertEqual(self._post(port).status, 200)

    def test_valid_token_relays(self):
        port = self._make_edge("s3cret")
        resp = self._post(port, "Bearer s3cret")
        self.assertEqual(resp.status, 200)
        self.assertEqual(self.marker_cls.last_auth, "Bearer s3cret")

    def test_missing_token_rejected_before_relay(self):
        port = self._make_edge("s3cret")
        resp = self._post(port)
        self.assertEqual(resp.status, 401)
        self.assertIsNone(self.marker_cls.last_auth, "rejected request must not reach proxy")

    def test_wrong_token_rejected(self):
        port = self._make_edge("s3cret")
        self.assertEqual(self._post(port, "Bearer wrong").status, 401)
        self.assertEqual(self._post(port, "bearer s3cret").status, 401)  # scheme is case-sensitive
        # whitespace-tolerant: paste artifacts (trailing space) still authenticate,
        # but a wrong token does not (2026-09-03: Warp users pasting keys with
        # trailing spaces got "wrong API key" despite a valid key)
        self.assertEqual(self._post(port, "Bearer s3cret ").status, 200)
        self.assertEqual(self._post(port, "Bearer  s3cret ").status, 200)
        self.assertEqual(self._post(port, "Bearer s3cretx").status, 401)
        # token side may carry whitespace from .env paste (edge normalizes both sides)
        self.assertEqual(self._post(port, "Bearer s3cret ").status, 200)
