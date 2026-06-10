"""ToolRecall HTTP Proxy — HTTP ↔ UDS Bridge.

The HTTP Proxy forwards requests to the ToolRecall Daemon over UDS.
It contains no caching logic — everything is routed through the Daemon.

Purpose: agents that only speak HTTP (Claude Code, Codex, Cursor, etc.)
can talk to ToolRecall via this bridge instead of UDS.

Hermes users: you don't need this — Hermes uses UDS/MCP directly.

Endpoints:
    GET /cached_read?path=       → cached_read via Daemon
    GET /cached_terminal?cmd=    → cached_terminal via Daemon
    GET /cached_skill?name=      → cached_skill via Daemon
    GET /docs_search?query=      → docs_search via Daemon
    GET /health                  → {"status": "ok"}
"""

import http.server
import json
import logging
import urllib.parse

from toolrecall.transport import TransportClient

log = logging.getLogger("toolrecall.proxy")


class ToolRecallHandler(http.server.BaseHTTPRequestHandler):
    """HTTP request handler — forwards to Daemon via UDS."""

    def __init__(self, *args, **kwargs):
        self._client = TransportClient()
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
                    result = self._client.send({"cmd": "cached_read", "path": p})

            elif path == "/cached_terminal":
                c = q.get("cmd", "")
                if not c:
                    result = {"error": "Missing 'cmd' query parameter"}
                else:
                    ttl_str = q.get("ttl", "0")
                    ttl = int(ttl_str) if ttl_str else None
                    result = self._client.send({"cmd": "cached_terminal", "command": c, "ttl": ttl})

            elif path == "/cached_skill":
                s = q.get("name", "")
                if not s:
                    result = {"error": "Missing 'name' query parameter"}
                else:
                    result = self._client.send({"cmd": "cached_skill", "name": s})

            elif path == "/docs_search":
                query = q.get("query", "")
                if not query:
                    result = {"error": "Missing 'query' query parameter"}
                else:
                    src = q.get("source", None)
                    result = self._client.send({"cmd": "docs_search", "query": query, "source": src})

            elif path == "/health":
                ping = self._client.send({"cmd": "ping"})
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

    def log_message(self, format, *args):
        """Suppress default request logging."""
        pass


def run_server(bind: str = "127.0.0.1", port: int = 8567):
    """Start the ToolRecall HTTP proxy bridge.

    Binds to localhost only (safe default). No network exposure.
    The proxy is a bridge for agents that only speak HTTP
    (Claude Code, Codex, Cursor, etc).
    Hermes uses UDS natively — no proxy needed.
    """
    try:
        server = http.server.HTTPServer(("127.0.0.1", port), ToolRecallHandler)
    except OSError as e:
        if e.errno == 98:  # Address already in use
            log.error("Port %d already in use — is another proxy running?", port)
            return
        raise

    log.info("ToolRecall HTTP Proxy running on http://%s:%d", bind, port)

    # Check daemon
    client = TransportClient()
    ping = client.send({"cmd": "ping"})
    if ping.get("error") == "daemon_unavailable":
        log.warning("ToolRecall daemon not running! Start with: toolrecall daemon &")
        log.info("Proxy started — will connect when daemon becomes available")
    else:
        log.info("Connected to ToolRecall daemon")

    log.info("Endpoints:")
    log.info("  GET /cached_read?path=/path/to/file")
    log.info("  GET /cached_terminal?cmd=<command>&ttl=<seconds>")
    log.info("  GET /cached_skill?name=skill-name")
    log.info("  GET /docs_search?query=<search terms>")
    log.info("  GET /health")
    log.info("Recommended: put nginx in front for SSL + auth.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Shutting down.")