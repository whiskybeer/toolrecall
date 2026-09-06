# Copyright (c) 2026 Robin Schultka
# SPDX-License-Identifier: MIT
# Source: https://github.com/whiskybeer/toolrecall
"""Full-chain test: Warp edge -> ToolRecall forward proxy -> live daemon -> mock upstream.

Proves the integration's value proposition on a real HTTP path:
1. Identical request twice -> 1 MISS (forwarded to upstream) + 1 HIT (served
   from daemon api_cache, upstream sees only ONE call).
2. HIT response carries X-ToolRecall-Cache: HIT.
3. Different body -> new MISS (no false sharing).

Needs a running ToolRecall daemon (skips otherwise). Mock upstream replaces
the real LLM API; the proxy is a real `toolrecall.proxy.ForwardProxyHandler`.
"""

import json
import sys
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

sys.path.insert(0, __file__.rsplit("/", 2)[0])

from toolrecall.adapters import warp
from toolrecall.transport import TransportClient, DEFAULT_PATH


class _MockLLMHandler(BaseHTTPRequestHandler):
    call_count = 0

    def log_message(self, fmt, *args):
        return

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        _MockLLMHandler.call_count += 1
        body = json.dumps(
            {
                "id": "chatcmpl-mock",
                "usage": {"prompt_tokens": 100, "completion_tokens": 10},
                "choices": [
                    {"message": {"content": f"upstream-call-{_MockLLMHandler.call_count}"}}
                ],
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.mark.allow_prod_paths  # intentionally exercises the LIVE daemon api_cache
class TestFullChain(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        ping = TransportClient(DEFAULT_PATH).send({"cmd": "ping"})
        if not ping.get("pong"):
            raise unittest.SkipTest("ToolRecall daemon not running")

        # Mock LLM API (stands in for api.openai.com)
        cls.llm = HTTPServer(("127.0.0.1", 0), _MockLLMHandler)
        threading.Thread(target=cls.llm.serve_forever, daemon=True).start()

        # Real TR forward proxy; PATH_ROUTES sends /v1/chat/completions to
        # api.openai.com. Redirect the proxy's outbound HTTPS connection to
        # our mock upstream (plain HTTP on a local port — http.client is
        # happy to speak HTTP over the intercepted socket).
        import http.client

        def redirected_connect(self):
            import socket

            self.sock = socket.create_connection(("127.0.0.1", cls.llm.server_port), self.timeout)

        cls._real_https_connect = http.client.HTTPSConnection.connect
        http.client.HTTPSConnection.connect = redirected_connect

        from toolrecall.proxy import ForwardProxyHandler

        cls.proxy = HTTPServer(("127.0.0.1", 0), ForwardProxyHandler)
        threading.Thread(target=cls.proxy.serve_forever, daemon=True).start()

        # Warp edge in front of the proxy
        cls.edge = warp.make_edge_server(
            "api.openai.com", proxy_host="127.0.0.1", proxy_port=cls.proxy.server_port
        )
        cls.edge_port = cls.edge.server_address[1]
        threading.Thread(target=cls.edge.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        import http.client

        http.client.HTTPSConnection.connect = cls._real_https_connect
        cls.edge.shutdown()
        cls.proxy.shutdown()
        cls.llm.shutdown()

    def _post(self, body: bytes):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.edge_port}/v1/chat/completions",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, dict(r.headers), r.read()
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), e.read()

    def test_miss_then_hit_single_upstream_call(self):
        import uuid

        _MockLLMHandler.call_count = 0
        probe = f"warp-e2e-probe-{uuid.uuid4()}"  # unique per run: daemon cache persists
        body = json.dumps(
            {
                "model": "gpt-4o-mini",
                "messages": [{"role": "user", "content": probe}],
            }
        ).encode()

        s1, h1, b1 = self._post(body)
        self.assertEqual(s1, 200)
        self.assertEqual(
            h1.get("X-ToolRecall-Cache"),
            "MISS",
            "first request with a fresh body must be a cache MISS",
        )
        self.assertEqual(_MockLLMHandler.call_count, 1, "first request must reach upstream")

        s2, h2, b2 = self._post(body)
        self.assertEqual(s2, 200)
        self.assertEqual(h2.get("X-ToolRecall-Cache"), "HIT")
        self.assertEqual(_MockLLMHandler.call_count, 1, "HIT must NOT re-call upstream")
        self.assertEqual(b1, b2, "cached body must be byte-identical")

        # Different body -> fresh MISS
        body2 = json.dumps(
            {
                "model": "gpt-4o-mini",
                "messages": [{"role": "user", "content": probe + "-variant"}],
            }
        ).encode()
        s3, _, _ = self._post(body2)
        self.assertEqual(s3, 200)
        self.assertEqual(_MockLLMHandler.call_count, 2, "different body must reach upstream")


if __name__ == "__main__":
    unittest.main()
