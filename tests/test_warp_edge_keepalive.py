# Copyright (c) 2026 Robin Schultka
# SPDX-License-Identifier: MIT
# Source: https://github.com/whiskybeer/toolrecall
"""Edge SSE keepalive: silent upstreams must not kill the client connection.

Live failure (2026-09-06): Warp's requests reach the edge through a backend
relay (GCP LB) that closes connections with no bytes for ~30s — exactly what
happens while a big-prompt generation thinks before its first token. The edge
now injects `: keepalive` SSE comment frames during silence. SSE clients
ignore comment frames; the data stream stays byte-exact.

Tests use TOOLRECALL_SSE_KEEPALIVE < 1s so silence windows are testable fast.
"""

import json
import os
import threading
import time
import urllib.request

import pytest

os.environ.setdefault("TOOLRECALL_SSE_KEEPALIVE", "0.3")

from toolrecall.adapters import warp as warp_mod  # noqa: E402


class _SilentThenStreamHandler:
    """Test upstream: hold the SSE response open for `silence` seconds with no
    bytes, then emit two data events and close."""

    silence = 1.0

    def __call__(self, handler):
        handler.send_response(200)
        handler.send_header("Content-Type", "text/event-stream")
        handler.send_header("Connection", "close")
        handler.end_headers()
        # silence window: no bytes at all
        time.sleep(self.silence)
        handler.wfile.write(b'data: {"delta":"one"}\n\n')
        handler.wfile.flush()
        time.sleep(0.1)
        handler.wfile.write(b"data: [DONE]\n\n")
        handler.wfile.flush()
        handler.close_connection = True


def _start_upstream(handler_fn):
    import http.server

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            return

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            self.rfile.read(length)
            handler_fn(self)

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


def _edge_request(port, body: bytes, timeout: float = 15.0):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/v1/chat/completions",
        data=body,
        headers={"Content-Type": "application/json", "Authorization": "Bearer t"},
        method="POST",
    )
    start = time.monotonic()
    frames = []
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        ctype = resp.headers.get("Content-Type", "")
        # read until EOF (Connection: close framing)
        while True:
            line = resp.fp.readline()
            if not line:
                break
            frames.append(line)
        return ctype, frames, time.monotonic() - start


@pytest.fixture()
def edge_port(monkeypatch):
    """Full edge server in front of a silent-then-streaming upstream."""
    srv, upstream_port = _start_upstream(_SilentThenStreamHandler())
    monkeypatch.setattr(warp_mod, "TOOLRECALL_PROXY_HOST", "127.0.0.1")
    monkeypatch.setattr(warp_mod, "_SSE_KEEPALIVE_INTERVAL", 0.3)

    esrv = warp_mod.make_edge_server(
        "openrouter.ai",
        proxy_host="127.0.0.1",
        proxy_port=upstream_port,
        auth_token="t",
    )
    port = esrv.server_address[1]
    threading.Thread(target=esrv.serve_forever, daemon=True).start()
    yield port
    esrv.shutdown()
    srv.shutdown()


class TestEdgeSSEKeepalive:
    def test_keepalive_frames_during_silence(self, edge_port):
        body = json.dumps(
            {"model": "m", "stream": True, "messages": [{"role": "user", "content": "hi"}]}
        ).encode()
        ctype, frames, elapsed = _edge_request(edge_port, body)
        assert "text/event-stream" in ctype
        raw = b"".join(frames)
        # keepalive comment frames arrived while upstream was silent
        assert b": keepalive" in raw
        # and the real data made it through byte-exact
        assert b'data: {"delta":"one"}' in raw
        assert b"data: [DONE]" in raw
        # first byte must have arrived BEFORE the upstream's first data byte
        # (that is the whole point — the client sees traffic during silence)
        first_keepalive_idx = raw.find(b": keepalive")
        first_data_idx = raw.find(b'data: {"delta"')
        assert first_keepalive_idx < first_data_idx

    def test_data_stream_unchanged(self, edge_port):
        body = json.dumps(
            {"model": "m", "stream": True, "messages": [{"role": "user", "content": "hi"}]}
        ).encode()
        _, frames, _ = _edge_request(edge_port, body)
        raw = b"".join(frames)
        # strip keepalive comment frames — they are transport noise
        lines = [ln for ln in raw.split(b"\n") if ln and not ln.startswith(b":")]
        data = [ln + b"\n" for ln in lines if ln.startswith(b"data:")]
        # exactly the two upstream data events, unmodified
        assert data == [b'data: {"delta":"one"}\n', b"data: [DONE]\n"]

    def test_no_keepalive_when_upstream_flows(self, monkeypatch):
        # upstream that streams immediately: no silence → no keepalive frames
        def fast(handler):
            handler.send_response(200)
            handler.send_header("Content-Type", "text/event-stream")
            handler.end_headers()
            handler.wfile.write(b"data: [DONE]\n\n")
            handler.wfile.flush()
            handler.close_connection = True

        srv, upstream_port = _start_upstream(fast)
        monkeypatch.setattr(warp_mod, "_SSE_KEEPALIVE_INTERVAL", 0.3)

        esrv = warp_mod.make_edge_server(
            "openrouter.ai",
            proxy_host="127.0.0.1",
            proxy_port=upstream_port,
            auth_token="t",
        )
        threading.Thread(target=esrv.serve_forever, daemon=True).start()
        try:
            body = json.dumps(
                {"model": "m", "stream": True, "messages": [{"role": "user", "content": "hi"}]}
            ).encode()
            _, frames, _ = _edge_request(esrv.server_address[1], body)
            raw = b"".join(frames)
            assert b": keepalive" not in raw
            assert b"data: [DONE]" in raw
        finally:
            esrv.shutdown()
            srv.shutdown()
