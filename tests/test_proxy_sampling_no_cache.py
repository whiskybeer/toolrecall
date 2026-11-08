"""Requests with an explicit temperature > 0 must not be served from cache.

A replayed sample is not an independent draw: best-of-N agents sending the
same body at temperature 0.7 got one sample N times (bench/results/
FLEET_RESULTS.md, Phase 0 check 2). Opt-in X-ToolRecall-Cache-Sampled: 1
keeps the old behaviour; absent temperature stays cached.
"""

import http.client
import http.server
import json
import os
import socket
import tempfile
import threading
import unittest

import pytest

import toolrecall.proxy as proxy_mod
from toolrecall.proxy import _explicit_sampling


@pytest.mark.parametrize(
    "body,expected",
    [
        ({"temperature": 0.7}, True),
        ({"temperature": 1}, True),
        ({"temperature": 0}, False),
        ({"temperature": 0.0}, False),
        ({}, False),
        ({"temperature": None}, False),
        ({"temperature": "0.7"}, False),
        ({"temperature": True}, False),
        ({"messages": [{"role": "user", "content": '"temperature": 0.9'}]}, False),
    ],
)
def test_explicit_sampling(body, expected):
    assert _explicit_sampling(json.dumps(body).encode()) is expected


def test_explicit_sampling_non_json():
    assert _explicit_sampling(b'"temperature": 0.7 not json') is False
    assert _explicit_sampling(b"[1, 2]") is False


class _Upstream(http.server.BaseHTTPRequestHandler):
    calls = 0

    def log_message(self, *a):
        return

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        _Upstream.calls += 1
        n = _Upstream.calls
        if b'"stream": true' in body:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            chunk = {"id": f"c{n}", "choices": [{"delta": {"content": f"sample-{n}"}}]}
            self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
            usage = {"choices": [], "usage": {"prompt_tokens": 3, "completion_tokens": 1}}
            self.wfile.write(f"data: {json.dumps(usage)}\n\n".encode())
            self.wfile.write(b"data: [DONE]\n\n")
            return
        out = json.dumps(
            {
                "id": f"c{n}",
                "choices": [{"message": {"role": "assistant", "content": f"sample-{n}"}}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 1},
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


HOST = "api.samplingtest.invalid"


@pytest.mark.allow_prod_paths
class TestSampledRequestsHTTP(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from tests.e2e_helpers import E2EDaemon

        os.environ["TOOLRECALL_MCP_ALLOW_INVALIDATE"] = "1"
        cls.daemon = E2EDaemon()
        cls.daemon.start()
        if not cls.daemon.client.send({"cmd": "ping"}).get("pong"):
            raise unittest.SkipTest("hermetic daemon failed to start")
        cls._orig_transport = os.environ.get("TOOLRECALL_TRANSPORT")
        os.environ["TOOLRECALL_TRANSPORT"] = cls.daemon.socket_path
        # never the production usage CSV
        cls._orig_usage = proxy_mod._USAGE_LOG_PATH
        proxy_mod._USAGE_LOG_PATH = os.path.join(tempfile.mkdtemp(), "proxy_usage.csv")
        proxy_mod._init_usage_log()
        cls._orig_allowed = proxy_mod._host_allowed
        proxy_mod._host_allowed = lambda h: h.startswith(HOST)
        cls.upstream = http.server.HTTPServer(("127.0.0.1", 0), _Upstream)
        threading.Thread(target=cls.upstream.serve_forever, daemon=True).start()
        cls._orig_connect = http.client.HTTPSConnection.connect
        port = cls.upstream.server_port

        def redirected(self):
            self.sock = socket.create_connection(("127.0.0.1", port), self.timeout)

        http.client.HTTPSConnection.connect = redirected
        cls.proxy = http.server.HTTPServer(("127.0.0.1", 0), proxy_mod.ForwardProxyHandler)
        threading.Thread(target=cls.proxy.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.proxy.shutdown()
        cls.upstream.shutdown()
        http.client.HTTPSConnection.connect = cls._orig_connect
        proxy_mod._host_allowed = cls._orig_allowed
        proxy_mod._USAGE_LOG_PATH = cls._orig_usage
        if cls._orig_transport is None:
            os.environ.pop("TOOLRECALL_TRANSPORT", None)
        else:
            os.environ["TOOLRECALL_TRANSPORT"] = cls._orig_transport
        cls.daemon.stop()

    def setUp(self):
        _Upstream.calls = 0
        self.daemon.client.send({"cmd": "cache_invalidate", "scope": "api"})

    def _post(self, body: dict, headers: dict | None = None):
        h = {"Host": HOST, "Content-Type": "application/json", **(headers or {})}
        conn = http.client.HTTPConnection("127.0.0.1", self.proxy.server_port, timeout=15)
        conn.request("POST", "/v1/chat/completions", body=json.dumps(body).encode(), headers=h)
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        return resp.getheader("X-ToolRecall-Cache"), data

    def _body(self, tag, **kw):
        return {"model": "m", "messages": [{"role": "user", "content": tag}], **kw}

    def test_default_transparent_caching_caches_sampled_requests(self):
        """By default (bypass_sampled=false), sampled requests ARE cached normally."""
        body = self._body("sampled-default", temperature=0.7)
        self.assertEqual(self._post(body)[0], "MISS")
        self.assertEqual(self._post(body)[0], "HIT")
        self.assertEqual(_Upstream.calls, 1)

    def test_header_bypass_sampled_reaches_upstream(self):
        """X-ToolRecall-Bypass-Sampled: 1 header bypasses cache for temperature > 0."""
        body = self._body("sampled-header", temperature=0.7)
        hdr = {"X-ToolRecall-Bypass-Sampled": "1"}
        h1, d1 = self._post(body, hdr)
        h2, d2 = self._post(body, hdr)
        self.assertEqual((h1, h2), ("NOCACHE", "NOCACHE"))
        self.assertEqual(_Upstream.calls, 2)
        self.assertNotEqual(d1, d2)

    def test_env_bypass_sampled_reaches_upstream(self):
        """TOOLRECALL_FORWARD_BYPASS_SAMPLED=1 bypasses cache for temperature > 0."""
        os.environ["TOOLRECALL_FORWARD_BYPASS_SAMPLED"] = "1"
        try:
            body = self._body("sampled-env", temperature=0.7)
            h1, d1 = self._post(body)
            h2, d2 = self._post(body)
            self.assertEqual((h1, h2), ("NOCACHE", "NOCACHE"))
            self.assertEqual(_Upstream.calls, 2)
            self.assertNotEqual(d1, d2)
        finally:
            os.environ.pop("TOOLRECALL_FORWARD_BYPASS_SAMPLED", None)

    def test_greedy_and_absent_temperature_still_cached(self):
        os.environ["TOOLRECALL_FORWARD_BYPASS_SAMPLED"] = "1"
        try:
            for i, kw in enumerate(({"temperature": 0}, {})):
                body = self._body(f"greedy-{i}", **kw)
                self.assertEqual(self._post(body)[0], "MISS")
                self.assertEqual(self._post(body)[0], "HIT")
            self.assertEqual(_Upstream.calls, 2)
        finally:
            os.environ.pop("TOOLRECALL_FORWARD_BYPASS_SAMPLED", None)

    def test_opt_in_header_caches_sampled_even_when_bypass_active(self):
        os.environ["TOOLRECALL_FORWARD_BYPASS_SAMPLED"] = "1"
        try:
            body = self._body("opt-in", temperature=0.7)
            opt = {"X-ToolRecall-Cache-Sampled": "1"}
            self.assertEqual(self._post(body, opt)[0], "MISS")
            self.assertEqual(self._post(body, opt)[0], "HIT")
            self.assertEqual(_Upstream.calls, 1)
        finally:
            os.environ.pop("TOOLRECALL_FORWARD_BYPASS_SAMPLED", None)

    def test_sampled_stream_skips_canonical_stream_cache(self):
        os.environ["TOOLRECALL_FORWARD_BYPASS_SAMPLED"] = "1"
        try:
            body = self._body("stream", temperature=0.7, stream=True)
            canon = {"X-ToolRecall-Canonicalize": "hermes"}
            _, d1 = self._post(body, canon)
            _, d2 = self._post(body, canon)
            self.assertEqual(_Upstream.calls, 2)
            self.assertIn(b"sample-1", d1)
            self.assertIn(b"sample-2", d2)
        finally:
            os.environ.pop("TOOLRECALL_FORWARD_BYPASS_SAMPLED", None)
