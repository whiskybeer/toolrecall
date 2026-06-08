"""ToolRecall HTTP Proxy — HTTP ↔ UDS Bridge.

The HTTP Proxy forwards HTTP requests to the ToolRecall Daemon.
It contains no independent caching logic, no SQLite, and no LRU memory —
everything is routed through the Daemon.

Endpoints:
    GET /cached_read?path=       → cached_read via Daemon
    GET /cached_terminal?cmd=    → cached_terminal via Daemon
    GET /cached_skill?name=      → cached_skill via Daemon
    GET /docs_search?query=      → docs_search via Daemon
    GET /health                  → {"status": "ok"}
"""

import http.server
import json
import urllib.parse

from toolrecall.client import UDSClient


class ToolRecallHandler(http.server.BaseHTTPRequestHandler):
    """HTTP request handler — leitet an Daemon weiter."""

    def __init__(self, *args, **kwargs):
        self._client = UDSClient()
        super().__init__(*args, **kwargs)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        params = urllib.parse.parse_qs(parsed.query)
        q = {k: v[0] if v else "" for k, v in params.items()}

        try:
            if path == "/cached_read":
                p = q.get("path", "")
                if not p:
                    result = {"error": "Missing 'path' query parameter"}
                else:
                    result = self._client._send({"cmd": "cached_read", "path": p})

            elif path == "/cached_terminal":
                c = q.get("cmd", "")
                if not c:
                    result = {"error": "Missing 'cmd' query parameter"}
                else:
                    ttl_str = q.get("ttl", "0")
                    ttl = int(ttl_str) if ttl_str else None
                    result = self._client._send({"cmd": "cached_terminal", "command": c, "ttl": ttl})

            elif path == "/cached_skill":
                s = q.get("name", "")
                if not s:
                    result = {"error": "Missing 'name' query parameter"}
                else:
                    result = self._client._send({"cmd": "cached_skill", "name": s})

            elif path == "/docs_search":
                query = q.get("query", "")
                if not query:
                    result = {"error": "Missing 'query' query parameter"}
                else:
                    src = q.get("source", None)
                    result = self._client._send({"cmd": "docs_search", "query": query, "source": src})

            elif path == "/health":
                ping = self._client._send({"cmd": "ping"})
                if ping.get("error") == "daemon_unavailable":
                    result = {"status": "error", "daemon": "not running"}
                    self.send_response(503)
                else:
                    result = {"status": "ok", "daemon": "connected", "version": "0.2.0"}
                    self.send_response(200)

            else:
                result = {"error": f"Unknown endpoint: {path}"}
                self.send_response(404)

            if "error" in result and path != "/health":
                self.send_response(500 if result["error"] != "daemon_unavailable" else 503)
            elif path == "/health":
                pass  # Already set
            else:
                self.send_response(200)

        except Exception as e:
            result = {"error": str(e)}
            self.send_response(500)

        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(result).encode())

    def log_message(self, fmt, *args):
        """Suppress default request logging."""
        pass


def run_server(bind: str = "127.0.0.1", port: int = 8567):
    """Start the ToolRecall HTTP proxy bridge."""
    import socket as sock_mod
    try:
        sock_mod.getaddrinfo(bind, port)
    except sock_mod.gaierror:
        print(f"Warning: '{bind}' does not resolve on this system.")
        print("Falling back to '127.0.0.1' (all interfaces).")
        print("Set TOOLRECALL_PROXY_BIND=127.0.0.1 for localhost-only.")
        bind = "127.0.0.1"

    server = http.server.HTTPServer((bind, port), ToolRecallHandler)
    print(f"ToolRecall HTTP Proxy (Daemon Bridge) running on http://{bind}:{port}")

    # Check daemon
    client = UDSClient()
    ping = client._send({"cmd": "ping"})
    if ping.get("error") == "daemon_unavailable":
        print("  ⚠ ToolRecall daemon not running! Start with: toolrecall daemon &")
    else:
        print("  ✓ Connected to ToolRecall daemon")

    print(f"Endpoints:")
    print(f"  GET /cached_read?path=/path/to/file")
    print(f"  GET /cached_terminal?cmd=<command>&ttl=<seconds>")
    print(f"  GET /cached_skill?name=skill-name")
    print(f"  GET /docs_search?query=<search terms>")
    print(f"  GET /health")
    print()
    print("Recommended: put nginx in front for SSL + auth.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")