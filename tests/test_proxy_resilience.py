"""E2E tests for proxy _forward() retry + circuit breaker (non-streaming).

Opt-in via [resilience]. Uses a mock upstream HTTP server; the proxy's
outbound HTTPS connect is redirected to it (same technique as
test_warp_fullchain.py).
"""

import http.server
import json
import os
import sys
import tempfile
import threading
import unittest

test_db_dir = tempfile.mkdtemp()
os.environ["TOOLRECALL_CACHE_DB"] = os.path.join(test_db_dir, "test_proxy_retry.db")
os.environ["TOOLRECALL_RETRY"] = "true"
os.environ["TOOLRECALL_RETRY_MAX_ATTEMPTS"] = "3"
os.environ["TOOLRECALL_RETRY_BACKOFF_BASE"] = "0"  # no sleeping in tests
os.environ["TOOLRECALL_CIRCUIT_BREAKER"] = "true"
os.environ["TOOLRECALL_CB_FAILURE_THRESHOLD"] = "3"
os.environ["TOOLRECALL_CB_WINDOW"] = "60"
os.environ["TOOLRECALL_CB_OPEN_SECONDS"] = "30"

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import toolrecall.proxy as proxy_mod  # noqa: E402


class _FlakyHandler(http.server.BaseHTTPRequestHandler):
    fail_times = 0  # number of initial requests that return 503
    seen = 0

    def do_POST(self):
        _FlakyHandler.seen += 1
        if _FlakyHandler.seen <= _FlakyHandler.fail_times:
            self.send_response(503)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error":"overloaded"}')
            return
        payload = json.dumps({"ok": True, "n": _FlakyHandler.seen}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, fmt, *args):  # silence
        pass


class _QuietHandler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        self.send_response(500)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"error":"always broken"}')

    def log_message(self, fmt, *args):
        pass


def _make_proxy(handler_cls, host="api.testretry.invalid"):
    """Build a proxy with SSRF allowlist patched to include the test host."""
    server = http.server.HTTPServer(("127.0.0.1", 0), proxy_mod.ForwardProxyHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _forward(proxy_port, body=None):
    import http.client

    if body is None:
        # Unique per call: the proxy's api_cache is keyed on
        # method:host:path:body, and a live daemon persists across test
        # runs — a static body would serve a stale HIT from a previous run
        # and never reach the mock upstream.
        import uuid

        body = json.dumps({"probe": uuid.uuid4().hex}).encode()
    conn = http.client.HTTPConnection("127.0.0.1", proxy_port, timeout=10)
    conn.request(
        "POST",
        "/v1/chat/completions",
        body=body,
        headers={"Host": "api.testretry.invalid:80", "Content-Type": "application/json"},
    )
    resp = conn.getresponse()
    data = resp.read()
    conn.close()
    return resp.status, data


class TestForwardRetry(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Allowlist the test host for _forward's SSRF sink guard
        cls._orig_allowed = proxy_mod._host_allowed
        proxy_mod._host_allowed = lambda h: h.startswith("api.testretry.invalid")
        cls.upstream = http.server.HTTPServer(("127.0.0.1", 0), _FlakyHandler)
        threading.Thread(target=cls.upstream.serve_forever, daemon=True).start()
        # Redirect outbound connect to the mock upstream
        cls._orig_connect = http.client.HTTPSConnection.connect

        def redirected(self):
            import socket

            self.sock = socket.create_connection(
                ("127.0.0.1", cls.upstream.server_port), self.timeout
            )

        http.client.HTTPSConnection.connect = redirected
        cls._redirected = redirected

    @classmethod
    def tearDownClass(cls):
        proxy_mod._host_allowed = cls._orig_allowed
        http.client.HTTPSConnection.connect = cls._orig_connect

    def setUp(self):
        _FlakyHandler.seen = 0
        _FlakyHandler.fail_times = 0
        # Fresh CB per test
        proxy_mod._forward_breaker = None

    def _proxy(self):
        return _make_proxy(proxy_mod.ForwardProxyHandler)

    def test_503_retried_then_success(self):
        _FlakyHandler.fail_times = 2
        p = self._proxy()
        try:
            status, data = _forward(p.server_port)
            self.assertEqual(status, 200, "retry must recover from 2x 503")
            self.assertIn(b'"ok": true', data)
            self.assertEqual(_FlakyHandler.seen, 3, "2 fails + 1 success")
        finally:
            p.shutdown()

    def test_no_retry_when_disabled(self):
        os.environ["TOOLRECALL_RETRY"] = "false"
        try:
            _FlakyHandler.fail_times = 2
            p = self._proxy()
            try:
                status, _ = _forward(p.server_port)
                self.assertEqual(status, 503, "retry off → first 503 passes through")
                self.assertEqual(_FlakyHandler.seen, 1)
            finally:
                p.shutdown()
        finally:
            os.environ["TOOLRECALL_RETRY"] = "true"

    def test_circuit_opens_after_threshold(self):
        # 30: enough that no test call can exhaust the failures — the 4th
        # call must fast-fail via the breaker, not recover via retry.
        _FlakyHandler.fail_times = 30
        p = self._proxy()
        try:
            bodies = [b'{"cbprobe":"%s"}' % os.urandom(8).hex().encode() for _ in range(4)]
            for i in range(3):
                status, _ = _forward(p.server_port, body=bodies[i])
                self.assertEqual(status, 503)
            # Breaker must now be OPEN → next call fast-fails without
            # reaching upstream
            seen_before = _FlakyHandler.seen
            status, data = _forward(p.server_port, body=bodies[3])
            self.assertEqual(status, 503)
            self.assertIn(b"circuit_open", data)
            self.assertEqual(_FlakyHandler.seen, seen_before, "CB open must not hit upstream")
        finally:
            p.shutdown()


if __name__ == "__main__":
    unittest.main()
