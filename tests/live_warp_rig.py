# Copyright (c) 2026 Robin Schultka
# SPDX-License-Identifier: MIT
"""Live test rig: real TR proxy + real edge, outbound redirected to a mock
upstream (no API key needed). Point a tunnel at the edge port and fire
identical requests through the public URL: expect MISS then HIT with
exactly one upstream call."""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import http.client

from toolrecall.proxy import ForwardProxyHandler
from toolrecall.adapters.warp import make_edge_server


class MockLLMHandler(BaseHTTPRequestHandler):
    call_count = 0

    def log_message(self, fmt, *args):
        return

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        MockLLMHandler.call_count += 1
        body = json.dumps(
            {
                "id": "chatcmpl-live",
                "usage": {"prompt_tokens": 100, "completion_tokens": 10},
                "choices": [{"message": {"content": f"upstream-call-{MockLLMHandler.call_count}"}}],
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main():
    mock = HTTPServer(("127.0.0.1", 0), MockLLMHandler)
    threading.Thread(target=mock.serve_forever, daemon=True).start()
    mock_port = mock.server_address[1]

    def redirected_connect(self):
        import socket

        self.sock = socket.create_connection(("127.0.0.1", mock_port), self.timeout)

    http.client.HTTPSConnection.connect = redirected_connect

    proxy = HTTPServer(("127.0.0.1", 0), ForwardProxyHandler)
    threading.Thread(target=proxy.serve_forever, daemon=True).start()

    edge = make_edge_server(
        "api.openai.com", proxy_host="127.0.0.1", proxy_port=proxy.server_address[1]
    )
    edge_port = edge.server_address[1]
    threading.Thread(target=edge.serve_forever, daemon=True).start()

    print(f"MOCK_UPSTREAM_PORT={mock_port}", flush=True)
    print(f"EDGE_PORT={edge_port}", flush=True)
    print(f"EDGE_URL=http://127.0.0.1:{edge_port}", flush=True)

    import signal

    signal.pause()


if __name__ == "__main__":
    main()
