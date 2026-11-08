"""STREAM usage-truth: the streaming passthrough parses the final SSE usage chunk.

Before this test file existed, STREAM rows in proxy_usage.csv logged
`prompt_tokens = len(body)//4` (a byte estimate) and `completion_tokens = 0`
always — the relay never looked at the stream bytes. Providers honoring
stream_options.include_usage (OpenRouter, DeepSeek, OpenAI, ...) emit one
final SSE chunk with the real usage block, which the proxy relayed but
discarded. These tests pin the billed-truth path:

1. _parse_sse_usage_tail unit behavior (found / absent / malformed / [DONE]).
2. End-to-end: a streaming upstream emitting a usage tail produces a CSV row
   with real prompt+completion+cache tokens, NOT the byte estimate.
3. Fallback: a stream without a usage chunk keeps the estimate behavior.

Hermeticity: upstream is a local mock; the proxy's daemon/cache path is inert
(no canonical profile header → no tee/store); usage log is pointed at a temp
CSV via patch of _USAGE_LOG_PATH.
"""

import http.client
import http.server
import json
import os
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import toolrecall.proxy as proxy_mod  # noqa: E402


def _sse(*chunks):
    """Build an SSE body: each arg is a dict to serialize, or a raw string."""
    lines = []
    for c in chunks:
        if isinstance(c, dict):
            lines.append(f"data: {json.dumps(c)}")
        else:
            lines.append(f"data: {c}")
    return "\n\n".join(lines) + "\n\n"


class _StreamHandler(http.server.BaseHTTPRequestHandler):
    """Mock upstream: replays canned SSE bytes for every POST."""

    sse_body = b""
    protocol_version = "HTTP/1.1"

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0) or 0))
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(self.sse_body)))
        self.end_headers()
        self.wfile.write(self.sse_body)

    def log_message(self, format, *args):  # noqa: A002 - stdlib signature
        pass


def _read_csv_rows(path):
    with open(path) as f:
        return [line.strip().split(",") for line in f if line.strip()]


class TestParseSseUsageTail(unittest.TestCase):
    def test_usage_chunk_found(self):
        tail = _sse(
            {"id": "x", "choices": []},
            {"usage": {"prompt_tokens": 123, "completion_tokens": 45}},
            "[DONE]",
        )
        u = proxy_mod._parse_sse_usage_tail(tail.encode())
        self.assertEqual(u["prompt_tokens"], 123)
        self.assertEqual(u["completion_tokens"], 45)

    def test_last_data_line_wins(self):
        # A delta chunk that happens to contain a "usage" key as a non-dict
        # (e.g. null) must not shadow the final real usage chunk.
        tail = _sse(
            {"choices": [{"delta": {"content": "hi"}}], "usage": None},
            {"choices": [], "usage": {"prompt_tokens": 7, "completion_tokens": 2}},
            "[DONE]",
        )
        u = proxy_mod._parse_sse_usage_tail(tail.encode())
        self.assertEqual(u["prompt_tokens"], 7)

    def test_no_usage_chunk_returns_empty(self):
        tail = _sse({"choices": [{"delta": {"content": "hi"}}]}, "[DONE]")
        self.assertEqual(proxy_mod._parse_sse_usage_tail(tail.encode()), {})

    def test_malformed_json_ignored(self):
        raw = "data: {broken json\n\ndata: [DONE]\n\n"
        self.assertEqual(proxy_mod._parse_sse_usage_tail(raw.encode()), {})

    def test_non_sse_bytes_return_empty(self):
        self.assertEqual(proxy_mod._parse_sse_usage_tail(b"<html>oops</html>"), {})

    def test_never_raises_on_any_garbage(self):
        for blob in (b"", b"\x00\xff\xfe", "data: [DONE]".encode(), "data: null\n\n".encode()):
            proxy_mod._parse_sse_usage_tail(blob)  # must not raise
        self.assertTrue(True)


class TestStreamBilledTruth(unittest.TestCase):
    """End-to-end: streaming relay logs real usage from the final SSE chunk."""

    @classmethod
    def setUpClass(cls):
        # Allowlist the test host for _forward's SSRF sink guard
        cls._orig_allowed = proxy_mod._host_allowed
        proxy_mod._host_allowed = lambda h: h.startswith("api.testretry.invalid")
        cls.upstream = http.server.HTTPServer(("127.0.0.1", 0), _StreamHandler)
        threading.Thread(target=cls.upstream.serve_forever, daemon=True).start()
        cls._orig_connect = http.client.HTTPSConnection.connect

        def redirected(self):
            import socket

            self.sock = socket.create_connection(
                ("127.0.0.1", cls.upstream.server_port), self.timeout
            )

        http.client.HTTPSConnection.connect = redirected

    @classmethod
    def tearDownClass(cls):
        proxy_mod._host_allowed = cls._orig_allowed
        http.client.HTTPSConnection.connect = cls._orig_connect

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="tr_test_stream_usage_")
        self.csv_path = os.path.join(self.tmpdir, "proxy_usage.csv")
        self._patches = [
            patch.object(proxy_mod, "_USAGE_LOG_PATH", self.csv_path),
            # Inert daemon: cached_api_check/store would otherwise touch the
            # live daemon socket. STREAM without canonical profile doesn't
            # call them, but the header is client-controlled — pin it shut.
            patch.object(
                proxy_mod.TransportClient,
                "send",
                lambda self, msg: {"_error": "daemon_unavailable"},
            ),
        ]
        for p in self._patches:
            p.start()
        _StreamHandler.sse_body = b""

    def tearDown(self):
        for p in self._patches:
            p.stop()
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _forward(self):
        import uuid

        # "stream": true is required — the proxy routes to the streaming
        # passthrough only when the request body matches _STREAM_RE.
        body = json.dumps({"probe": uuid.uuid4().hex, "stream": True}).encode()
        conn = http.client.HTTPConnection("127.0.0.1", self._port, timeout=10)
        conn.request(
            "POST",
            "/v1/chat/completions",
            body=body,
            headers={"Host": "api.testretry.invalid:80", "Content-Type": "application/json"},
        )
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        return resp, data

    def _proxy_port(self):
        server = http.server.HTTPServer(("127.0.0.1", 0), proxy_mod.ForwardProxyHandler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self._port = server.server_port
        return server.server_port

    def test_final_usage_chunk_logged_as_billed_truth(self):
        usage = {
            "prompt_tokens": 66992,
            "completion_tokens": 454,
            "total_tokens": 67446,
            "cost": 0.0051379,
            "prompt_tokens_details": {"cached_tokens": 3210304},
        }
        _StreamHandler.sse_body = _sse(
            {"id": "a", "choices": [{"delta": {"content": "Hel"}}]},
            {"id": "b", "choices": [{"delta": {"content": "lo"}}]},
            {"id": "c", "choices": [], "usage": usage},
            "[DONE]",
        ).encode()
        self._proxy_port()
        resp, data = self._forward()
        self.assertEqual(resp.status, 200)
        # Client still received the full stream verbatim
        self.assertIn(b'"prompt_tokens": 66992', data)
        self.assertIn(b"[DONE]", data)
        rows = _read_csv_rows(self.csv_path)
        self.assertEqual(len(rows), 1)
        _ts, status, host, path, _h, pt, ct, crt, _cwt, cost, _dd, _cd = rows[0]
        self.assertEqual(status, "STREAM")
        self.assertEqual(host, "api.testretry.invalid:80")
        self.assertEqual(pt, "66992")  # real, not len(body)//4 (~30)
        self.assertEqual(ct, "454")  # was always 0 before this fix
        # v4 contract: the nested prompt_tokens_details.cached_tokens flows
        # into cache_read_tokens (OpenRouter's actual reporting shape).
        self.assertEqual(crt, "3210304")
        # Provider-billed dollars land in the row (OpenRouter usage.cost),
        # written with 6-decimal precision
        self.assertAlmostEqual(float(cost), 0.0051379, places=6)

    def test_no_usage_chunk_falls_back_to_estimate(self):
        _StreamHandler.sse_body = _sse(
            {"id": "a", "choices": [{"delta": {"content": "hello world"}}]},
            "[DONE]",
        ).encode()
        self._proxy_port()
        self._forward()
        rows = _read_csv_rows(self.csv_path)
        self.assertEqual(len(rows), 1)
        _ts, status, _h, _p, _h2, pt, ct, _crt, _cwt, cost, _dd, _cd = rows[0]
        self.assertEqual(status, "STREAM")
        self.assertEqual(ct, "0")
        self.assertGreater(int(pt), 0)  # len(body)//4 estimate still logged
        # No provider cost reported → 0 in the column (absent ≠ guessed)
        self.assertEqual(float(cost), 0.0)

    def test_cache_read_tokens_recorded_when_present(self):
        usage = {
            "prompt_tokens": 100000,
            "completion_tokens": 200,
            "prompt_tokens_details": {
                "cached_tokens": 95000,
                "cache_write_tokens": 500,
            },
        }
        _StreamHandler.sse_body = _sse(
            {"choices": [], "usage": usage},
            "[DONE]",
        ).encode()
        self._proxy_port()
        self._forward()
        rows = _read_csv_rows(self.csv_path)
        self.assertEqual(rows[0][5], "100000")
        self.assertEqual(rows[0][6], "200")
        # v4 contract: nested prompt_tokens_details.cached_tokens IS read
        # (flat OpenAI/Anthropic fields first, nesting as fallback).
        self.assertEqual(rows[0][7], "95000")
        self.assertEqual(rows[0][8], "500")
        self.assertEqual(rows[0][11], "0.000000")  # no discount in usage → 0

    def test_cost_usd_column_v2_header_and_legacy_rows(self):
        # The writer emits 10 columns with the v2 header; legacy 9-column rows
        # (pre-2026-09-10) must stay readable via DictReader (cost → None → 0).
        # Seed the CSV first (writer creates it on first log; this test writes
        # directly, so seed the v2 header ourselves).
        with open(self.csv_path, "w") as f:
            f.write(proxy_mod._USAGE_LOG_HEADER)
        with open(self.csv_path) as f:
            header = f.readline().strip()
        self.assertIn(",cost_usd", header)
        # Simulate a legacy row: 9 fields, no cost
        with open(self.csv_path, "a") as f:
            f.write("1789045342.847,MISS,h,p,hash,10,5,0,0\n")
        import csv as _csv

        with open(self.csv_path) as f:
            legacy = [r for r in _csv.DictReader(f) if r["request_hash"] == "hash"]
        self.assertEqual(len(legacy), 1)
        self.assertEqual(int(legacy[0].get("cost_usd") or 0), 0)

    def test_bad_cost_value_degrades_to_zero(self):
        usage = {"prompt_tokens": 10, "completion_tokens": 1, "cost": "not-a-number"}
        _StreamHandler.sse_body = _sse(
            {"choices": [], "usage": usage},
            "[DONE]",
        ).encode()
        self._proxy_port()
        self._forward()
        rows = _read_csv_rows(self.csv_path)
        self.assertEqual(float(rows[0][9]), 0.0)

    def test_multi_chunk_large_stream_tail_bounded(self):
        # 50KB of delta noise then the usage chunk — parser only needs the
        # last bytes; huge bodies must not accumulate unboundedly.
        filler = {"choices": [{"delta": {"content": "x" * 1000}}]}
        _StreamHandler.sse_body = _sse(
            *([filler] * 50),
            {"choices": [], "usage": {"prompt_tokens": 42, "completion_tokens": 1}},
            "[DONE]",
        ).encode()
        self._proxy_port()
        self._forward()
        rows = _read_csv_rows(self.csv_path)
        self.assertEqual(rows[0][5], "42")
        self.assertEqual(rows[0][6], "1")


if __name__ == "__main__":
    unittest.main()
