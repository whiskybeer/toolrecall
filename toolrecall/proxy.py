"""ToolRecall Forward Proxy — cache API responses without touching the provider.

Intercepts HTTP requests to LLM providers (OpenAI, Anthropic, Google, DeepSeek, etc.)
by matching the Host header. On repeat requests with identical bodies, returns the
cached response — no API call, no token cost.

Architecture:
    Agent SDK → Forward Proxy (port 8569, set OPENAI_BASE_URL / ANTHROPIC_BASE_URL)
      → Cache HIT: respond from api_cache table
      → Cache MISS: forward to real API, store response, return

No MITM needed — works by redirecting the base URL in your SDK config,
preserving all original headers (Authorization, Content-Type) and body intact.

Usage:
    export OPENAI_BASE_URL=http://localhost:8569/v1
    export ANTHROPIC_BASE_URL=http://localhost:8569
    # Or set any SDK's base_url to http://localhost:8569
    # Then use your agent/scripts normally — API responses are cached automatically.
"""

import hashlib
import http.client
import http.server
import json
import logging
import os
import sys

from toolrecall.transport import TransportClient

log = logging.getLogger("toolrecall.proxy")

# Maximum POST body size (5 MB) — prevents OOM from malicious payloads
# or misconfigured clients sending multi-GB blobs to a localhost process.
MAX_BODY_SIZE = 5 * 1024 * 1024

# Known LLM API hosts that the forward proxy routes requests for.
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

# Path-based routing: maps path prefixes to API hosts.
# Used when the SDK sends Host: localhost (OPENAI_BASE_URL=http://localhost:8569).
PATH_ROUTES = {
    "api.openai.com": "/v1",
    "api.anthropic.com": "/v1",
    "api.deepseek.com": "/v1",
    "api.x.ai": "/v1",
    "api.mistral.ai": "/v1",
    "api.groq.com": "/v1",
    "api.together.xyz": "/v1",
    "openrouter.ai": "/v1",
    "generativelanguage.googleapis.com": "/v1beta",
}


class ForwardProxyHandler(http.server.BaseHTTPRequestHandler):
    """Forward proxy handler that caches API responses via ToolRecall daemon.

        Receives requests pointed at this proxy via OPENAI_BASE_URL / ANTHROPIC_BASE_URL
        or by setting any SDK's base URL to http://localhost:8569.
        Matches the Host header against FORWARD_HOSTS, hashes the request
        body, checks the api_cache, and either returns cached responses
        or forwards to the real API and caches the result.

    No MITM needed — works by URL redirection, preserving
    all original headers (Authorization, Content-Type) and body intact.
    """

    def __init__(self, *args, **kwargs):
        self._client = TransportClient()
        super().__init__(*args, **kwargs)

    # ── Generic dispatch ────────────────────────────────

    def _handle(self, method: str):
        """Handle any HTTP method (GET, POST, etc.) via forwarding proxy.

        Resolves the real target host from:
          1. X-Target-Host header (explicit override, for SDK usage)
          2. Host header (works with curl -H "Host: api.openai.com")
          3. Path-based routing: /v1/chat/completions → api.openai.com
        """
        target_host = (
            self.headers.get("X-Target-Host")
            or self.headers.get("Host", "")
        )
        target_path = self.path

        # Path-based routing fallback: when Host is localhost (SDK redirect),
        # infer the real API host from the path prefix.
        if not target_host or target_host.split(":")[0] in ("localhost", "127.0.0.1"):
            for known_host, path_prefix in PATH_ROUTES.items():
                if target_path.startswith(path_prefix):
                    target_host = known_host
                    break

        target_scheme = "https"

        # Only cache known API hosts
        is_known_host = target_host in FORWARD_HOSTS

        if not is_known_host:
            self._forward_direct(method, target_host, target_path, target_scheme)
            return

        body_bytes = b""
        content_length = int(self.headers.get("Content-Length", 0))
        is_streaming = False
        if content_length > 0:
            if content_length > MAX_BODY_SIZE:
                self.send_response(413)
                self.end_headers()
                self.wfile.write(b'{"error":"Request too large"}')
                return
            body_bytes = self.rfile.read(content_length)
            # Detect streaming requests — bypass cache, use chunked relay
            if body_bytes and b'"stream": true' in body_bytes:
                is_streaming = True

        # Build cache key: hash(method + host + path + body)
        body_hash = hashlib.sha256(body_bytes).hexdigest()
        request_str = f"{method}:{target_host}:{target_path}:{body_hash}"
        request_hash = hashlib.sha256(request_str.encode()).hexdigest()

        # Streaming requests: bypass cache entirely, use chunked passthrough
        if is_streaming:
            log.info(
                "🔴 STREAM: %s %s%s — bypassing cache, chunked relay",
                method, target_host, target_path,
            )
            self._forward_streaming(method, target_host, target_path, target_scheme, body_bytes)
            return

        # Check cache — only serve cached 2xx responses
        cached = self._client.send({
            "cmd": "cached_api_check",
            "request_hash": request_hash,
        })
        if cached.get("cached"):
            status = cached.get("status", 200)
            # Don't replay non-2xx responses even if cached
            if status < 200 or status >= 300:
                log.warning("Skipping cached non-2xx response (status %d) for %s %s%s",
                            status, method, target_host, target_path)
            else:
                log.info(
                    "✅ API CACHE HIT: %s %s%s (hash=%s, saved ~%s tokens)",
                    method, target_host, target_path,
                    request_hash[:12], cached.get("tokens_saved", "?"),
                )
                self.send_response(status)
                for hdr_key, hdr_val in cached.get("headers", {}).items():
                    if hdr_key.lower() not in ("transfer-encoding", "content-encoding", "content-length"):
                        self.send_header(hdr_key, hdr_val)
                self.send_header("X-ToolRecall-Cache", "HIT")
                self.end_headers()
                cached_body = cached["body"]
                self.wfile.write(cached_body.encode("utf-8") if isinstance(cached_body, str) else cached_body)
                return

        # Cache MISS — forward to real API
        log.info("❌ API CACHE MISS: %s %s%s — forwarding...", method, target_host, target_path)
        resp_status, resp_headers, resp_body = self._forward(
            method, target_host, target_path, target_scheme, body_bytes,
        )

        # Store in cache — only cache 2xx responses
        if 200 <= resp_status < 300:
            # Convert headers list[tuple] to dict for JSON transport
            headers_dict = {}
            for k, v in resp_headers:
                if k.lower() not in headers_dict:
                    headers_dict[k] = v  # first wins (preserves Content-Type etc.)
            # Body must be str for JSON transport (api_cache schema stores TEXT)
            body_str = resp_body.decode("utf-8", errors="replace") if isinstance(resp_body, bytes) else resp_body
            self._client.send({
                "cmd": "cached_api_store",
                "request_hash": request_hash,
                "method": method,
                "host": target_host,
                "path": target_path,
                "request_body_hash": body_hash,
                "response_status": resp_status,
                "response_headers": headers_dict,
                "response_body": body_str,
                "ttl": 300,
            })

        # Respond
        self.send_response(resp_status)
        for k, v in resp_headers:
            if k.lower() not in ("transfer-encoding", "content-encoding"):
                self.send_header(k, v)
        self.send_header("X-ToolRecall-Cache", "MISS")
        self.end_headers()
        self.wfile.write(resp_body if isinstance(resp_body, bytes) else resp_body.encode("utf-8"))

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

        Returns (status_code, list_of_headers, body_bytes).
        """
        # SECURITY: Never fall back to plaintext HTTP for known API hosts.
        # The previous code caught ALL exceptions and fell back to HTTPConnection,
        # silently sending Authorization headers over unencrypted transport.
        # All hosts in FORWARD_HOSTS are HTTPS-only.
        try:
            conn = http.client.HTTPSConnection(host, timeout=30)
        except Exception as e:
            log.error("Cannot establish HTTPS connection to %s: %s", host, e)
            return 502, [("Content-Type", "application/json")], json.dumps({
                "error": f"HTTPS connection failed: {e}",
            })

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
            # SECURITY: Read body as raw bytes — previously decoded as UTF-8
            # with errors="replace", corrupting binary responses with U+FFFD.
            # Now stored and served as bytes to preserve fidelity.
            resp_body = resp.read()
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
            # SECURITY: Same MAX_BODY_SIZE protection as the API host path.
            # Previously this method had no size check, allowing OOM attacks.
            if cl > MAX_BODY_SIZE:
                self.send_response(413)
                self.end_headers()
                self.wfile.write(b'{"error":"Request too large"}')
                return
            body_bytes = self.rfile.read(cl)
        status, headers, body = self._forward(method, host, path, scheme, body_bytes)
        self.send_response(status)
        for k, v in headers:
            if k.lower() not in ("transfer-encoding", "content-encoding"):
                self.send_header(k, v)
        self.end_headers()
        # Body is now bytes — write directly.
        self.wfile.write(body if isinstance(body, bytes) else body.encode("utf-8"))

    def _forward_streaming(self, method: str, host: str, path: str,
                           scheme: str, body: bytes):
        """Forward request and relay response as chunked/streaming.

        Reads the upstream response line by line (SSE) and writes each
        chunk to the client immediately. No caching, no buffering.
        """
        import http.client
        try:
            conn = http.client.HTTPSConnection(host, timeout=30)
        except Exception as e:
            log.error("Cannot establish HTTPS connection to %s: %s", host, e)
            self.send_response(502)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": f"HTTPS connection failed: {e}"}).encode())
            return

        headers = {}
        skip_headers = {"host", "connection", "proxy-connection",
                        "transfer-encoding", "content-length"}
        for k, v in self.headers.items():
            if k.lower() not in skip_headers:
                headers[k] = v

        try:
            conn.request(method, path, body=body or None, headers=headers)
            resp = conn.getresponse()

            # Relay status line
            self.send_response(resp.status)

            # Relay headers, dropping transfer-encoding (we'll use chunked)
            for k, v in resp.getheaders():
                if k.lower() not in ("transfer-encoding", "content-encoding",
                                     "content-length", "connection"):
                    self.send_header(k, v)
            self.send_header("X-ToolRecall-Cache", "STREAM")
            self.send_header("X-ToolRecall-Stream", "passthrough")
            self.end_headers()

            # Relay body chunk by chunk — SSE lines or raw bytes
            buf = resp if hasattr(resp, "readline") else resp
            while True:
                chunk = resp.read(4096)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()

            conn.close()
        except Exception as e:
            log.error("Streaming forward failed for %s %s%s: %s", method, host, path, e)
            # If we already sent headers, we can't change status
            # Log the error and let the connection drop
            try:
                self.wfile.write(b"\n\n[ToolRecall streaming error]\n")
            except Exception:
                pass

    def log_message(self, format, *args):
        log.debug("ForwardProxy: " + format, *args)


def run_forward_proxy(bind: str = "127.0.0.1", port: int = None):
    """Start the ToolRecall forward proxy (caching API responses).

    Port priority:
      1. --port CLI argument
      2. TOOLRECALL_FORWARD_PORT env var
      3. 8569 (default)

    Binds to localhost only (safe default). No network exposure.
    On cache hit, returns the cached response directly — no API call, no token cost.
    """
    if port is None:
        port = int(os.environ.get("TOOLRECALL_FORWARD_PORT", "8569"))
    try:
        server = http.server.HTTPServer((bind, port), ForwardProxyHandler)
        actual_port = server.server_port
    except OSError as e:
        import errno
        if e.errno == errno.EADDRINUSE:
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


# ─── Debug/Demo Server ──────────────────────────────
# Minimal HTTP server for quick speed demos and debugging.
# 4 endpoints: /read, /term, /stats, /health
# Not a full bridge — just curl-friendly cache access.


class DebugHandler(http.server.BaseHTTPRequestHandler):
    """Minimal HTTP handler for debugging and demos — 4 endpoints."""

    def __init__(self, *args, **kwargs):
        self._client = TransportClient()
        super().__init__(*args, **kwargs)

    def do_GET(self):
        from urllib.parse import urlparse, parse_qs

        parsed = urlparse(self.path)
        path = parsed.path
        params = parse_qs(parsed.query)
        q = {k: v[0] if v else "" for k, v in params.items()}

        try:
            if path == "/read":
                p = q.get("path", "")
                if not p:
                    result = {"error": "Missing 'path' param"}
                else:
                    result = self._client.send({"cmd": "cached_read", "path": p})

            elif path == "/term":
                c = q.get("cmd", "")
                if not c:
                    result = {"error": "Missing 'cmd' param"}
                else:
                    result = self._client.send({"cmd": "cached_terminal", "command": c})

            elif path == "/stats":
                result = self._client.send({"cmd": "cache_status"})

            elif path == "/health":
                ping = self._client.send({"cmd": "ping"})
                if ping.get("error") == "daemon_unavailable":
                    self.send_response(503)
                    result = {"status": "error", "daemon": "not running"}
                else:
                    self.send_response(200)
                    result = {"status": "ok"}

            else:
                self.send_response(404)
                result = {"error": f"Unknown: {path}"}

            if "error" in result and path != "/health":
                self.send_response(500 if result["error"] != "daemon_unavailable" else 503)

            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(result).encode())

        except Exception as e:
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": str(e)}).encode())

    def log_message(self, format, *args):
        log.debug("DebugServer: " + format, *args)


def run_debug_server(bind: str = "127.0.0.1", port: int = 8570):
    """Start minimal debug/demo server on localhost (:8570).

    Endpoints:
      GET /read?path=X   → cached_read
      GET /term?cmd=X    → cached_terminal
      GET /stats         → cache statistics
      GET /health        → daemon status
    """
    try:
        server = http.server.HTTPServer((bind, port), DebugHandler)
        actual_port = server.server_port
    except OSError as e:
        if e.errno == 98:
            log.error("Port %d already in use", port)
            return
        raise

    print(f"ToolRecall Debug Server on http://{bind}:{actual_port}")
    log.info("Endpoints: GET /read?path=  GET /term?cmd=  GET /stats  GET /health")
    sys.stdout.flush()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Shutting down.")
