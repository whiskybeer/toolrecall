"""Tests for forward-proxy request-body dedup ([forward_proxy] dedup, default OFF).

Layers tested:
  1. `_maybe_dedup_body` unit behavior: disabled = passthrough, enabled =
     stubs duplicates, keep-first/protect-last, deterministic, fail-open.
  2. Full HTTP round trip through `ForwardProxyHandler` with a mock upstream
     (same redirect technique as test_proxy_resilience.py): non-streaming
     and SSE streaming, flag on/off, opt-out header, and parity with the
     LiteLLM adapter binding of the same core.
"""

import http.server
import json
import os
import sys
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

os.environ.setdefault("TOOLRECALL_CACHE_DB", "/tmp/toolrecall_dedup_test.db")

import toolrecall.proxy as proxy_mod  # noqa: E402
from toolrecall.adapters.litellm import dedup_messages as adapter_dedup  # noqa: E402
from toolrecall.dedup import dedup_messages as core_dedup  # noqa: E402

FILE_BLOCK = "def cache_lookup(key):\n    ...\n" * 1000  # ~2.9K chars


def _chat_body(dups: bool = True, stream: bool = False) -> dict:
    """5-message body with the duplicate at index 4 (inside protect_last=2).

    Used for protect-tail and no-duplicate cases; `_chat_body_stubbable()`
    covers the stub case.
    """
    messages = [
        {"role": "system", "content": "You are a code reviewer."},
        {"role": "user", "content": "Review cache.py"},
        {"role": "tool", "content": FILE_BLOCK},
        {"role": "user", "content": "Now re-check it"},
        {"role": "tool", "content": FILE_BLOCK if dups else FILE_BLOCK + "x"},
    ]
    body = {"model": "test-model", "messages": messages}
    if stream:
        body["stream"] = True
    return body


def _chat_body_stubbable() -> dict:
    """7 messages so the duplicate (index 4) is OUTSIDE protect_last=2."""
    return {
        "model": "test-model",
        "messages": [
            {"role": "system", "content": "You are a code reviewer."},
            {"role": "user", "content": "Review cache.py"},
            {"role": "tool", "content": FILE_BLOCK},  # first
            {"role": "user", "content": "Now re-check it"},
            {"role": "tool", "content": FILE_BLOCK},  # dup → stub
            {"role": "assistant", "content": "Checked."},
            {"role": "user", "content": "Summarize."},
        ],
    }


def _clear_dedup_env():
    for k in (
        "TOOLRECALL_FORWARD_DEDUP",
        "TOOLRECALL_DEDUP_MIN_CHARS",
        "TOOLRECALL_DEDUP_PROTECT_LAST",
    ):
        os.environ.pop(k, None)


def _dedup_off():
    """Pin dedup OFF regardless of any ambient config (`dedup = true` in the
    user's live toolrecall.toml must not flip these tests)."""
    os.environ["TOOLRECALL_FORWARD_DEDUP"] = "false"


class TestMaybeDedupBody(unittest.TestCase):
    """Unit: _maybe_dedup_body — flag matrix + fail-open."""

    def setUp(self):
        _clear_dedup_env()
        _dedup_off()

    def tearDown(self):
        _clear_dedup_env()

    def test_disabled_by_default(self):
        body = json.dumps(_chat_body_stubbable()).encode()
        out, stats = proxy_mod._maybe_dedup_body(body)
        self.assertEqual(out, body)
        self.assertIsNone(stats)

    def test_enabled_stubs_duplicates(self):
        os.environ["TOOLRECALL_FORWARD_DEDUP"] = "true"
        body = json.dumps(_chat_body_stubbable()).encode()
        out, stats = proxy_mod._maybe_dedup_body(body)
        self.assertIsNotNone(stats)
        self.assertEqual(stats["blocks"], 1)
        self.assertIn(b"[toolrecall-dedup]", out)
        # envelope keys preserved, valid JSON
        env = json.loads(out.decode())
        self.assertEqual(env["model"], "test-model")
        self.assertEqual(len(env["messages"]), 7)
        # keep-first: message 2 intact; stubbed message 4
        self.assertEqual(env["messages"][2]["content"], FILE_BLOCK)
        self.assertTrue(env["messages"][4]["content"].startswith("[toolrecall-dedup]"))
        # determinism
        out2, stats2 = proxy_mod._maybe_dedup_body(body)
        self.assertEqual(out2, out)
        self.assertEqual(stats2, stats)

    def test_no_duplicates_passthrough(self):
        os.environ["TOOLRECALL_FORWARD_DEDUP"] = "true"
        body = json.dumps(_chat_body(dups=False)).encode()
        out, stats = proxy_mod._maybe_dedup_body(body)
        self.assertIsNone(stats)
        self.assertEqual(out, body)

    def test_protect_last_never_stubs_tail(self):
        os.environ["TOOLRECALL_FORWARD_DEDUP"] = "true"
        # duplicate at index 4 of 5 → inside protect_last=2 → untouched
        body = json.dumps(_chat_body(dups=True)).encode()
        out, stats = proxy_mod._maybe_dedup_body(body)
        self.assertIsNone(stats)
        self.assertEqual(out, body)

    def test_fails_open_on_garbage(self):
        os.environ["TOOLRECALL_FORWARD_DEDUP"] = "true"
        for garbage in (b"not json", b"[1,2,3]", b'{"messages": "not-a-list"}', b'{"a": 1}'):
            out, stats = proxy_mod._maybe_dedup_body(garbage)
            self.assertEqual(out, garbage)
            self.assertIsNone(stats)

    def test_below_min_chars_untouched(self):
        os.environ["TOOLRECALL_FORWARD_DEDUP"] = "true"
        os.environ["TOOLRECALL_DEDUP_MIN_CHARS"] = "50000"
        body = json.dumps(_chat_body_stubbable()).encode()
        out, stats = proxy_mod._maybe_dedup_body(body)
        self.assertIsNone(stats)
        self.assertEqual(out, body)

    def test_non_ascii_roundtrip(self):
        os.environ["TOOLRECALL_FORWARD_DEDUP"] = "true"
        block = "prüfsumme–ü – " * 400  # multibyte
        body = {
            "model": "test-model",
            "messages": [
                {"role": "system", "content": "x"},
                {"role": "user", "content": "y"},
                {"role": "tool", "content": block},  # first
                {"role": "user", "content": "z"},
                {"role": "tool", "content": block},  # dup → stub
                {"role": "assistant", "content": "done"},  # spacer (protect_last=2)
                {"role": "user", "content": "w"},
            ],
        }
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
        out, stats = proxy_mod._maybe_dedup_body(raw)
        self.assertIsNotNone(stats)
        env = json.loads(out.decode("utf-8"))
        self.assertEqual(env["messages"][2]["content"], block)
        self.assertIn("[toolrecall-dedup]", env["messages"][4]["content"])
        # multibyte content must survive the JSON round trip byte-exact
        self.assertEqual(
            env["messages"][2]["content"], json.loads(raw.decode("utf-8"))["messages"][2]["content"]
        )


class _UpstreamHandler(http.server.BaseHTTPRequestHandler):
    """Mock upstream: echoes whether the received body contained a stub."""

    last_body = None
    last_headers = None

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        _UpstreamHandler.last_body = self.rfile.read(length)
        _UpstreamHandler.last_headers = dict(self.headers)
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        payload = json.dumps(
            {
                "id": "chatcmpl-mock",
                "choices": [{"message": {"role": "assistant", "content": "ok"}, "index": 0}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 1},
            }
        ).encode()
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, fmt, *args):  # silence
        pass


class _SSEUpstreamHandler(_UpstreamHandler):
    """Mock upstream for streaming: emits an SSE relay of the received body."""

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        _UpstreamHandler.last_body = self.rfile.read(length)
        _UpstreamHandler.last_headers = dict(self.headers)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        self.wfile.write(b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n')
        self.wfile.write(b'data: {"usage": {"prompt_tokens": 10, "completion_tokens": 1}}\n\n')
        self.wfile.write(b"data: [DONE]\n\n")


def _forward(proxy_port, body: bytes, extra_headers: dict | None = None):
    import http.client

    headers = {"Host": "api.deduptest.invalid", "Content-Type": "application/json"}
    headers.update(extra_headers or {})
    conn = http.client.HTTPConnection("127.0.0.1", proxy_port, timeout=10)
    conn.request("POST", "/v1/chat/completions", body=body, headers=headers)
    resp = conn.getresponse()
    data = resp.read()
    conn.close()
    return resp, data


class _ProxyHTTPTest(unittest.TestCase):
    """Shared harness: proxy with SSRF allowlist + upstream redirect."""

    upstream_cls = _UpstreamHandler

    @classmethod
    def setUpClass(cls):
        # Billed-truth hygiene: this harness drives the real ForwardProxyHandler,
        # which appends one row per request to _USAGE_LOG_PATH — by default the
        # PRODUCTION file ~/.toolrecall/proxy_usage.csv. Unpinned, every run of
        # this class leaked fake rows into it (observed 2026-09-26: 1106 rows,
        # host api.deduptest.invalid). Pin to a per-class temp file and restore
        # on teardown — same contract as tests/test_usage_dedup_column.py.
        import tempfile

        cls._orig_usage_path = proxy_mod._USAGE_LOG_PATH
        cls._usage_dir = tempfile.mkdtemp(prefix="tr_proxydedup_usage_")
        proxy_mod._USAGE_LOG_PATH = os.path.join(cls._usage_dir, "proxy_usage.csv")
        cls._orig_allowed = proxy_mod._host_allowed
        proxy_mod._host_allowed = lambda h: h.startswith("api.deduptest.invalid")
        cls.upstream = http.server.HTTPServer(("127.0.0.1", 0), cls.upstream_cls)
        threading.Thread(target=cls.upstream.serve_forever, daemon=True).start()
        cls._orig_connect = http.client.HTTPSConnection.connect
        import socket

        def redirected(self):
            self.sock = socket.create_connection(
                ("127.0.0.1", cls.upstream.server_port), self.timeout
            )

        http.client.HTTPSConnection.connect = redirected

    @classmethod
    def tearDownClass(cls):
        import shutil

        proxy_mod._host_allowed = cls._orig_allowed
        http.client.HTTPSConnection.connect = cls._orig_connect
        proxy_mod._USAGE_LOG_PATH = cls._orig_usage_path
        shutil.rmtree(cls._usage_dir, ignore_errors=True)
        cls.upstream.shutdown()

    def setUp(self):
        _clear_dedup_env()
        _dedup_off()
        _UpstreamHandler.last_body = None

    def tearDown(self):
        _clear_dedup_env()

    def _proxy(self):
        server = http.server.HTTPServer(("127.0.0.1", 0), proxy_mod.ForwardProxyHandler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        return server


class TestProxyDedupHTTP(_ProxyHTTPTest):
    """HTTP e2e: flag off = byte passthrough; flag on = upstream sees stubs."""

    def test_flag_off_upstream_sees_original_body(self):
        p = self._proxy()
        body = json.dumps(_chat_body_stubbable()).encode()
        resp, _ = _forward(p.server_port, body)
        self.assertEqual(resp.status, 200)
        self.assertEqual(_UpstreamHandler.last_body, body)
        self.assertNotIn(b"toolrecall-dedup", _UpstreamHandler.last_body)

    def test_flag_on_upstream_sees_stubbed_body(self):
        os.environ["TOOLRECALL_FORWARD_DEDUP"] = "true"
        p = self._proxy()
        body = json.dumps(_chat_body_stubbable()).encode()
        resp, _ = _forward(p.server_port, body)
        self.assertEqual(resp.status, 200)
        received = json.loads(_UpstreamHandler.last_body.decode())
        self.assertTrue(received["messages"][4]["content"].startswith("[toolrecall-dedup]"))
        self.assertEqual(received["messages"][2]["content"], FILE_BLOCK)
        # framing: Content-Length matched the REWRITTEN body (upstream parsed it)
        self.assertEqual(len(received["messages"]), 7)

    def test_no_dedup_header_bypasses(self):
        os.environ["TOOLRECALL_FORWARD_DEDUP"] = "true"
        p = self._proxy()
        body = json.dumps(_chat_body_stubbable()).encode()
        resp, _ = _forward(p.server_port, body, {"X-ToolRecall-No-Dedup": "1"})
        self.assertEqual(resp.status, 200)
        self.assertEqual(_UpstreamHandler.last_body, body)

    def test_non_chat_paths_untouched(self):
        os.environ["TOOLRECALL_FORWARD_DEDUP"] = "true"
        p = self._proxy()
        body = json.dumps({"input": FILE_BLOCK * 2}).encode()  # embeddings-shaped
        conn = http.client.HTTPConnection("127.0.0.1", p.server_port, timeout=10)
        conn.request(
            "POST",
            "/v1/embeddings",
            body=body,
            headers={"Host": "api.deduptest.invalid", "Content-Type": "application/json"},
        )
        resp = conn.getresponse()
        resp.read()
        conn.close()
        self.assertEqual(resp.status, 200)
        self.assertEqual(_UpstreamHandler.last_body, body)

    def test_adapter_parity_same_core(self):
        """The proxy path and the LiteLLM adapter binding produce the same output."""
        os.environ["TOOLRECALL_FORWARD_DEDUP"] = "true"
        p = self._proxy()
        body = json.dumps(_chat_body_stubbable()).encode()
        _forward(p.server_port, body)
        received = json.loads(_UpstreamHandler.last_body.decode())
        expected, _stats = adapter_dedup(_chat_body_stubbable()["messages"])
        self.assertEqual(received["messages"], expected)
        core_out, _ = core_dedup(_chat_body_stubbable()["messages"])
        self.assertEqual(received["messages"], core_out)


class TestProxyDedupStreaming(_ProxyHTTPTest):
    """Streaming (Hermes's default path): dedup applies BEFORE the SSE relay."""

    upstream_cls = _SSEUpstreamHandler

    def test_flag_on_stream_receives_stubbed_body(self):
        os.environ["TOOLRECALL_FORWARD_DEDUP"] = "true"
        p = self._proxy()
        body = json.dumps({**_chat_body_stubbable(), "stream": True}).encode()
        resp, data = _forward(p.server_port, body)
        self.assertEqual(resp.status, 200)
        received = json.loads(_UpstreamHandler.last_body.decode())
        self.assertTrue(received["messages"][4]["content"].startswith("[toolrecall-dedup]"))
        self.assertIn(b"toolrecall-dedup", _UpstreamHandler.last_body)
        # client still got the SSE stream through
        self.assertIn(b"data:", data)
        self.assertIn(b"[DONE]", data)

    def test_flag_off_stream_byte_exact(self):
        p = self._proxy()
        body = json.dumps({**_chat_body_stubbable(), "stream": True}).encode()
        resp, _ = _forward(p.server_port, body)
        self.assertEqual(resp.status, 200)
        self.assertEqual(_UpstreamHandler.last_body, body)


class TestDedupConfigKeys(unittest.TestCase):
    """Config plumbing: ENV_MAP key, bool coercion, known-key validation."""

    def setUp(self):
        _clear_dedup_env()

    def tearDown(self):
        _clear_dedup_env()

    def test_env_map_entry(self):
        from toolrecall.config import ENV_MAP

        self.assertEqual(ENV_MAP.get("TOOLRECALL_FORWARD_DEDUP"), ("forward_proxy", "dedup"))

    def test_env_bool_coercion(self):
        from toolrecall.config import load_config

        for raw, expected in (("true", True), ("false", False), ("1", True), ("0", False)):
            os.environ["TOOLRECALL_FORWARD_DEDUP"] = raw
            val = load_config().get("forward_proxy", "dedup")
            self.assertEqual(val, expected, f"env={raw!r}")

    def test_default_config_has_dedup_false(self):
        from toolrecall.toml_serializer import DEFAULT_CONFIG

        self.assertIn("dedup", DEFAULT_CONFIG.get("forward_proxy", {}))
        self.assertIs(DEFAULT_CONFIG["forward_proxy"]["dedup"], False)

    def test_toml_flag_read(self):
        os.environ["TOOLRECALL_CONFIG"] = self._write_toml()
        try:
            from toolrecall.config import load_config

            self.assertIs(load_config().get("forward_proxy", "dedup"), True)
        finally:
            os.environ.pop("TOOLRECALL_CONFIG", None)

    @staticmethod
    def _write_toml() -> str:
        import tempfile

        fd, path = tempfile.mkstemp(suffix=".toml")
        with os.fdopen(fd, "w") as f:
            f.write("[forward_proxy]\ndedup = true\nport = 8569\n")
        return path


if __name__ == "__main__":
    unittest.main()
