"""ToolRecall HTTP Proxy — HTTP ↔ UDS Bridge + Forward Proxy Mode.

The HTTP Proxy forwards requests to the ToolRecall Daemon over UDS.
It contains no caching logic — everything is routed through the Daemon.

Purpose: agents that only speak HTTP (Claude Code, Codex, Cursor, etc.)
can talk to ToolRecall via this bridge instead of UDS.

Hermes users: you don't need this — Hermes uses UDS/MCP directly.

Browser extension: uses this proxy to check/store cached page content.

Forward Proxy Mode (--forward):
  Runs on a configurable upstream-facing port (default 8080).
  Intercepts API calls to LLM providers (OpenAI, Anthropic, Google, etc.)
  by matching the Host header. On repeat requests with identical bodies,
  returns the cached response — no API call, no token cost.

  Architecture:
    Browser Extension (DNR redirect) → Forward Proxy (port 8080)
      → Cache HIT: respond from api_cache table
      → Cache MISS: forward to real API, store response, return

Endpoints (bridge mode):
    GET /cached_read?path=               → cached_read via Daemon
    GET /cached_terminal?cmd=            → cached_terminal via Daemon
    GET /cached_skill?name=              → cached_skill via Daemon
    GET /cached_mcp_check?key=           → cached_mcp_check via Daemon
    POST /cached_mcp_store               → cached_mcp_store via Daemon
    GET /cached_browser_check?key=       → cached_browser_check via Daemon
    POST /cached_browser_store           → cached_browser_store via Daemon
    GET /docs_search?query=              → docs_search via Daemon
    GET /health                          → {"status": "ok"}
"""

import hashlib
import http.client
import http.server
import json
import logging
import os
import sys
import urllib.parse

from toolrecall.transport import TransportClient

log = logging.getLogger("toolrecall.proxy")

# Maximum POST body size (5 MB) — prevents OOM from malicious payloads
# or misconfigured clients sending multi-GB blobs to a localhost process.
MAX_BODY_SIZE = 5 * 1024 * 1024

# Known LLM API hosts that the forward proxy routes requests for.
# The browser extension declares DNR rules matching exactly these hosts.
FORWARD_HOSTS = {
    "api.openai.com",
    "api.anthropic.com",
    "generativelanguage.googleapis.com",
    "api.deepseek.com",
    "api.x.ai",
    "api.mistral.ai",
    "api.groq.com",
    "api.together.xyz",
    "openrouter.ai",
}


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

            elif path == "/cached_mcp_check":
                key = q.get("key", "")
                if not key:
                    result = {"error": "Missing 'key' query parameter"}
                else:
                    result = self._client.send({"cmd": "cached_mcp_check", "key": key})

            elif path == "/cached_browser_check":
                key = q.get("key", "")
                if not key:
                    result = {"error": "Missing 'key' query parameter"}
                else:
                    result = self._client.send({"cmd": "cached_browser_check", "cache_key": key})

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

            elif path == "/cache/stats":
                result = self._client.send({"cmd": "cache_status"})

            elif path == "/cache/invalidate":
                result = self._client.send({"cmd": "cache_invalidate"})

            elif path == "/cache/invalidate_file":
                p = q.get("path", "")
                if not p:
                    result = {"error": "Missing 'path' query parameter"}
                else:
                    result = self._client.send({"cmd": "cache_refresh_file", "path": p})

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

        self._send_json(result)

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        content_length = int(self.headers.get("Content-Length", 0))

        # Enforce max body size BEFORE reading
        if content_length > MAX_BODY_SIZE:
            self.send_response(413)
            self._send_json({"error": f"Request body too large (max {MAX_BODY_SIZE} bytes)"})
            return

        body = self.rfile.read(content_length)
        try:
            data = json.loads(body) if body else {}
        except json.JSONDecodeError:
            self.send_response(400)
            self._send_json({"error": "Invalid JSON body"})
            return

        try:
            if path == "/cached_mcp_store":
                key = data.get("key", "")
                content = data.get("content", "")
                if not key:
                    result = {"error": "Missing 'key' in body"}
                else:
                    result = self._client.send({
                        "cmd": "cached_mcp_store",
                        "key": key,
                        "content": content,
                        "url": data.get("url", ""),
                        "contentType": data.get("contentType", ""),
                    })

            elif path == "/cached_browser_store":
                cache_key = data.get("key", data.get("cache_key", ""))
                content = data.get("content", "")
                if not cache_key or not content:
                    result = {"error": "Missing 'key'/'content' in body"}
                else:
                    result = self._client.send({
                        "cmd": "cached_browser_store",
                        "cache_key": cache_key,
                        "content": content,
                        "url": data.get("url", ""),
                        "content_type": data.get("contentType", data.get("content_type", "snapshot")),
                        "title": data.get("title", ""),
                        "content_hash": data.get("content_hash", ""),
                    })

            else:
                result = {"error": f"Unknown POST endpoint: {path}"}
                self.send_response(404)

            if "error" in result:
                self.send_response(500)
            else:
                self.send_response(200)

        except Exception as e:
            result = {"error": str(e)}
            self.send_response(500)

        self._send_json(result)

    def _send_json(self, data: dict):
        """Send JSON response with appropriate headers."""
        self.send_header("Content-Type", "application/json")
        # No CORS header: proxy binds only to localhost (127.0.0.1).
        # Access-Control-Allow-Origin: * is pointless on a local service
        # and risky if accidentally bound to network (CSRF on /cache/invalidate).
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

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
        server = http.server.HTTPServer((bind, port), ToolRecallHandler)
        actual_port = server.server_port
    except OSError as e:
        if e.errno == 98:  # Address already in use
            log.error("Port %d already in use — is another proxy running?", port)
            return
        raise

    log.info("ToolRecall HTTP Proxy running on http://%s:%d", bind, actual_port)
    # Print the actual port to stdout for detection by external tools
    # Format: "http://127.0.0.1:PORT" — only meaningful when bind==127.0.0.1
    print(f"http://127.0.0.1:{actual_port}")
    sys.stdout.flush()

    # Check daemon
    client = TransportClient()
    ping = client.send({"cmd": "ping"})
    if ping.get("error") == "daemon_unavailable":
        log.warning("ToolRecall daemon not running! Start with: toolrecall daemon &")
        log.info("Proxy started — will connect when daemon becomes available")
    else:
        log.info("Connected to ToolRecall daemon")

    log.info("Endpoints:")
    log.info("  GET  /cached_read?path=/path/to/file")
    log.info("  GET  /cached_terminal?cmd=<command>&ttl=<seconds>")
    log.info("  GET  /cached_skill?name=skill-name")
    log.info("  GET  /cached_mcp_check?key=<cache-key>")
    log.info("  POST /cached_mcp_store")
    log.info("  GET  /cached_browser_check?key=<cache-key>")
    log.info("  POST /cached_browser_store")
    log.info("  GET  /docs_search?query=<search terms>")
    log.info("  GET  /health")
    log.info("Recommended: put nginx in front for SSL + auth.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Shutting down.")


# ─── Forward Proxy Mode ──────────────────────────────────


class ForwardProxyHandler(http.server.BaseHTTPRequestHandler):
    """Forward proxy that caches API responses via ToolRecall daemon.

    Receives requests redirected by the browser extension (DNR).
    Matches the Host header against FORWARD_HOSTS, hashes the request
    body, checks the api_cache, and either returns cached responses
    or forwards to the real API and caches the result.

    No MITM needed — the browser extension redirects the URL, preserving
    all original headers (Authorization, Content-Type) and body intact.
    """

    def __init__(self, *args, **kwargs):
        self._client = TransportClient()
        super().__init__(*args, **kwargs)

    # ── Generic dispatch ────────────────────────────────

    def _handle(self, method: str):
        """Handle any HTTP method (GET, POST, etc.) via forwarding proxy."""
        target_host = self.headers.get("Host", "")
        target_path = self.path
        target_scheme = "https"

        # Only cache known API hosts
        is_known_host = target_host in FORWARD_HOSTS

        if not is_known_host:
            self._forward_direct(method, target_host, target_path, target_scheme)
            return

        body_bytes = b""
        content_length = int(self.headers.get("Content-Length", 0))
        if content_length > 0:
            if content_length > MAX_BODY_SIZE:
                self.send_response(413)
                self.end_headers()
                self.wfile.write(b'{"error":"Request too large"}')
                return
            body_bytes = self.rfile.read(content_length)

        # Build cache key: hash(method + host + path + body)
        body_hash = hashlib.sha256(body_bytes).hexdigest()
        request_str = f"{method}:{target_host}:{target_path}:{body_hash}"
        request_hash = hashlib.sha256(request_str.encode()).hexdigest()

        # Check cache
        cached = self._client.send({
            "cmd": "cached_api_check",
            "request_hash": request_hash,
        })
        if cached.get("cached"):
            log.info(
                "✅ API CACHE HIT: %s %s%s (hash=%s, saved ~%s tokens)",
                method, target_host, target_path,
                request_hash[:12], cached.get("tokens_saved", "?"),
            )
            self.send_response(cached.get("status", 200))
            for hdr_key, hdr_val in cached.get("headers", {}).items():
                if hdr_key.lower() not in ("transfer-encoding", "content-encoding", "content-length"):
                    self.send_header(hdr_key, hdr_val)
            self.send_header("X-ToolRecall-Cache", "HIT")
            self.end_headers()
            self.wfile.write(cached["body"].encode("utf-8"))
            return

        # Cache MISS — forward to real API
        log.info("❌ API CACHE MISS: %s %s%s — forwarding...", method, target_host, target_path)
        resp_status, resp_headers, resp_body = self._forward(
            method, target_host, target_path, target_scheme, body_bytes,
        )

        # Store in cache
        self._client.send({
            "cmd": "cached_api_store",
            "request_hash": request_hash,
            "method": method,
            "host": target_host,
            "path": target_path,
            "request_body_hash": body_hash,
            "response_status": resp_status,
            "response_headers": dict(resp_headers),
            "response_body": resp_body,
            "ttl": 300,
        })

        # Respond
        self.send_response(resp_status)
        for k, v in resp_headers:
            if k.lower() not in ("transfer-encoding", "content-encoding"):
                self.send_header(k, v)
        self.send_header("X-ToolRecall-Cache", "MISS")
        self.end_headers()
        self.wfile.write(resp_body.encode("utf-8") if isinstance(resp_body, str) else resp_body)

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def do_PUT(self):
        self._handle("PUT")

    def do_DELETE(self):
        self._handle("DELETE")

    def do_PATCH(self):
        self._handle("PATCH")

    def _forward(self, method: str, host: str, path: str,
                 scheme: str, body: bytes) -> tuple:
        """Forward request to the real API server.

        Returns (status_code, list_of_headers, body_string).
        """
        try:
            conn = http.client.HTTPSConnection(host, timeout=30)
        except Exception:
            conn = http.client.HTTPConnection(host, timeout=30)

        # Copy headers, dropping the ones we shouldn't forward
        headers = {}
        skip_headers = {"host", "connection", "proxy-connection",
                        "transfer-encoding", "content-length"}
        for k, v in self.headers.items():
            if k.lower() not in skip_headers:
                headers[k] = v

        try:
            conn.request(method, path, body=body or None, headers=headers)
            resp = conn.getresponse()
            resp_body = resp.read().decode("utf-8", errors="replace")
            resp_headers = resp.getheaders()
            conn.close()
            return resp.status, resp_headers, resp_body
        except Exception as e:
            log.error("Forward failed for %s %s%s: %s", method, host, path, e)
            return 502, [("Content-Type", "application/json")], json.dumps({
                "error": f"Forward failed: {e}",
            })

    def _forward_direct(self, method: str, host: str, path: str, scheme: str):
        """Forward request uncached (for non-API hosts)."""
        body_bytes = b""
        cl = int(self.headers.get("Content-Length", 0))
        if cl > 0:
            body_bytes = self.rfile.read(cl)
        status, headers, body = self._forward(method, host, path, scheme, body_bytes)
        self.send_response(status)
        for k, v in headers:
            if k.lower() not in ("transfer-encoding", "content-encoding"):
                self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body.encode("utf-8") if isinstance(body, str) else body)

    def log_message(self, format, *args):
        log.debug("ForwardProxy: " + format, *args)


def run_forward_proxy(bind: str = "127.0.0.1", port: int = 8080):
    """Start the ToolRecall forward proxy (caching API responses).

    This proxy intercepts API calls to LLM providers (OpenAI, Anthropic, etc.)
    redirected by the browser extension. On cache hit, returns the cached
    response directly — no API call, no token cost.

    Binds to localhost only (safe default). No network exposure.
    The forward proxy is intended for local use with the browser extension.
    """
    try:
        server = http.server.HTTPServer((bind, port), ForwardProxyHandler)
        actual_port = server.server_port
    except OSError as e:
        if e.errno == 98:
            log.error("Port %d already in use", port)
            return
        raise

    log.info("ToolRecall Forward Proxy running on http://%s:%d", bind, actual_port)
    print(f"Forward proxy: http://{bind}:{actual_port}")
    sys.stdout.flush()

    # Check daemon
    client = TransportClient()
    ping = client.send({"cmd": "ping"})
    if ping.get("error") == "daemon_unavailable":
        log.warning("ToolRecall daemon not running! Start with: toolrecall daemon &")
        log.info("Forward proxy started — will cache when daemon becomes available")
    else:
        log.info("Connected to ToolRecall daemon")

    log.info("Forwarding for %d known API hosts:", len(FORWARD_HOSTS))
    for h in sorted(FORWARD_HOSTS):
        log.info("  • %s", h)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Shutting down.")