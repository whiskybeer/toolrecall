# Copyright (c) 2026 Robin Schultka
# SPDX-License-Identifier: MIT
# Source: https://github.com/whiskybeer/toolrecall
"""Local HTTPS test server for the Antigravity × ToolRecall test kit.

Zero third-party egress: every HTTP call the kit makes goes to
https://127.0.0.1:<port> served by THIS module. The kit bundles its own
self-signed certificate (local_cert.pem, SAN: IP 127.0.0.1 + DNS localhost,
10-year validity). Clients pin that exact certificate — no verification
skipping, no certificate-store modification, no system trust changes.

Endpoints (all deterministic, stdlib only):

  /delay/<ms>?seed=&slot=    sleeps <ms>, then 200 + JSON body with the
                             query params echoed (latency simulation,
                             ETag = hash of the body → stable across calls)

  /etag/<id>                 200 + strong ETag "<id>" — change the <id>
                             path segment between calls to simulate a
                             content change (ETag differs → next
                             conditional GET yields 200 instead of 304)

  /flaky/<n>/<status>        returns <status> on the first <n> calls,
                             200 afterwards (failure-injection; unused by
                             default, available for edge-case demos)

  /counter                   JSON {"count": N} — counts requests since
                             server start (server-side truth that a cache
                           is actually intercepting calls)

Bind: 127.0.0.1 only (SSRF posture mirrors mcp_fetch — the fetch tool's
URL validator allows loopback only in these tests; the production tool
blocks it). Port: 0 = kernel-assigned (default); logged on start.

Security properties:
  - TLS 1.2+ with a bundled cert; clients pin by fingerprint (not
    trust-on-first-use, not verify-off)
  - Binds loopback only; no external interface
  - No filesystem access; no subprocess; stdlib only
  - Deterministic bodies — same args, same bytes, byte-for-byte

Usage:
    python local_server.py            # runs until killed; prints port
    python local_server.py --port 8642 --ms 800
"""

from __future__ import annotations

import argparse
import hashlib
import json
import ssl
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_BIND = "127.0.0.1"


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "ToolRecallKit/1.0"

    # ---- counters (server-side truth) ----
    counts: dict[str, int] = {}

    def _count(self, key: str) -> int:
        self.__class__.counts[key] = self.__class__.counts.get(key, 0) + 1
        return self.__class__.counts[key]

    def _send(self, status: int, body: bytes, headers: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):  # quiet by default
        pass

    # ---- routes ----
    def do_GET(self):  # noqa: N802 — http.server API
        from urllib.parse import urlparse, parse_qs

        parsed = urlparse(self.path)
        parts = [p for p in parsed.path.split("/") if p]
        qs = parse_qs(parsed.query)

        # /delay/<ms>?seed=&slot=  — latency simulation + param echo
        if len(parts) == 2 and parts[0] == "delay":
            try:
                ms = int(parts[1])
            except ValueError:
                ms = 200
            ms = max(0, min(ms, 10_000))
            time.sleep(ms / 1000.0)
            # ETag = hash of the STABLE core only. server_count must stay out:
            # it changes every request, which would make the validator never
            # match (found live in the first 304 smoke run).
            core = json.dumps(
                {
                    "endpoint": "delay",
                    "ms": ms,
                    "seed": (qs.get("seed") or [""])[0],
                    "slot": (qs.get("slot") or [""])[0],
                },
                sort_keys=True,
            ).encode()
            etag = hashlib.sha256(core).hexdigest()[:32]
            inm = self.headers.get("If-None-Match")
            if inm and inm.strip() == f'"{etag}"':
                self._count("delay:304")
                self._send(304, b"", {"ETag": f'"{etag}"'})
                return
            body = json.dumps(
                {
                    "endpoint": "delay",
                    "ms": ms,
                    "seed": (qs.get("seed") or [""])[0],
                    "slot": (qs.get("slot") or [""])[0],
                    "server_count": self._count(f"delay:{parsed.query}"),
                }
            ).encode()
            self._send(200, body, {"ETag": f'"{etag}"'})
            return

        # /etag/<id> — ETag demo: change <id> to simulate content change
        if len(parts) == 2 and parts[0] == "etag":
            ident = parts[1]
            self._count(f"etag:{ident}")
            # Conditional GET against the CURRENT id: only 304 if the client's
            # validator equals this route's current content id.
            inm = (self.headers.get("If-None-Match") or "").strip()
            if inm == f'"{ident}"':
                self._send(304, b"", {"ETag": f'"{ident}"'})
                return
            body = json.dumps({"endpoint": "etag", "id": ident}).encode()
            self._send(200, body, {"ETag": f'"{ident}"'})
            return

        # /flaky/<n>/<status> — fail first n calls with <status>, then 200
        if len(parts) == 3 and parts[0] == "flaky":
            try:
                n, status = int(parts[1]), int(parts[2])
            except ValueError:
                n, status = 0, 500
            seen = self._count("flaky")
            if seen <= n:
                self._send(status, json.dumps({"error": "flaky", "seen": seen}).encode())
                return
            self._send(200, json.dumps({"endpoint": "flaky", "seen": seen}).encode())
            return

        # /counter — how many requests since start (per-path buckets)
        self._count("counter")
        self._send(200, json.dumps({"count": sum(self.__class__.counts.values())}).encode())
        return


def make_server(port: int = 0, ms_default: int = 0) -> ThreadingHTTPServer:
    """Bind loopback-only ThreadingHTTPServer with TLS."""
    httpd = ThreadingHTTPServer((_BIND, port), _Handler)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(_cert_path())
    # ThreadingHTTPServer ships a plain TCP socket — wrap it or every client
    # gets WRONG_VERSION_NUMBER (exactly what the first smoke run showed).
    httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
    return httpd


def _cert_path() -> str:
    import os

    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "local_cert.pem")


def serve_in_thread(port: int = 0) -> tuple[ThreadingHTTPServer, int]:
    """Start the server on a background thread; returns (server, port)."""
    httpd = make_server(port)
    actual = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    return httpd, actual


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--port", type=int, default=0, help="0 = kernel-assigned")
    ap.add_argument("--cert", default=None, help="override bundled cert path")
    args = ap.parse_args()

    httpd = make_server(args.port)
    actual = httpd.server_address[1]
    print(f"ToolRecall test server on https://127.0.0.1:{actual}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
