"""ToolRecall Fetch MCP Server — stdlib-only HTTP fetcher.

Replacement for `uvx mcp-server-fetch` (Node.js heavy).
Zero dependencies, pure Python stdlib.

Supports: fetch_url (GET), fetch_head (HEAD), and fetch_headers (debug).
Returns: status, headers, content, content_type, encoding, truncated flag.

Content size limit:
  - Default: 500KB (512 * 1024 bytes).
  - Configurable via TOOLRECALL_FETCH_MAX_BYTES env var.
  - This is a HARD safety cap: user-supplied max_bytes is capped at this.
  - Set to 0 to disable the limit (not recommended — no RAM protection).
  - If the response exceeds the limit, the excess is truncated silently
    and truncated=True is returned.
  - This protects the MCP daemon from large responses that would consume
    all available memory (no streaming in stdio MCP).

SSRF protection (F14):
  - _validate_url resolves the target host and rejects non-public
    addresses (loopback, link-local/IMDS, private ranges) — numeric and
    IPv6-mapped spellings included.
  - Redirects are NOT followed: HTTPError for 3xx is surfaced to the
    caller instead (a redirect target could point at an internal
    address that was never validated).
"""

import json
import sys
import logging
import os
import ipaddress
import socket
from urllib.request import Request, build_opener, HTTPRedirectHandler
from urllib.error import URLError, HTTPError
from urllib.parse import urlparse

_LOG: logging.Logger | None = None

# Carrier-grade NAT range (RFC 6598) — NOT covered by ipaddress.is_private.
_CGNAT = ipaddress.ip_network("100.64.0.0/10")

# Maximum content bytes to fetch from a URL.
# Configurable via TOOLRECALL_FETCH_MAX_BYTES env var (in bytes).
# This is a HARD safety limit: any user-supplied max_bytes is capped at this.
# Default: 512 * 1024 = 500KB. Set to 0 for no limit (not recommended).
# What this means: if a URL returns more data than this limit, the excess
# is silently truncated — you get the first N bytes, and truncated=True.
# Common values:
#   100KB  = 100 * 1024        —  quick API calls, small pages
#   1MB   = 1024 * 1024       —  larger pages, API responses
#   10MB  = 10 * 1024 * 1024  —  bigger files (no streaming, beware RAM!)
MAX_CONTENT_BYTES = int(os.environ.get("TOOLRECALL_FETCH_MAX_BYTES", str(512 * 1024)))
if MAX_CONTENT_BYTES < 0:
    MAX_CONTENT_BYTES = 0  # no safety limit (not recommended)

TIMEOUT_SECONDS = 30
ALLOWED_SCHEMES = ("http", "https")

TOOLS = [
    {
        "name": "fetch_url",
        "description": "Fetch a URL and return its content. Supports HTTP/HTTPS. "
        "Content is truncated at the configured max_bytes limit. "
        "Returns: status, headers, content.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "URL to fetch (http/https only)"},
                "max_bytes": {
                    "type": "integer",
                    "description": "Max bytes to read (default: configured max limit, capped at that limit)",
                },
                "raw": {
                    "type": "boolean",
                    "description": "Return raw bytes instead of attempting text decode",
                },
            },
            "required": ["url"],
        },
    },
    {
        "name": "fetch_head",
        "description": "Fetch only HTTP headers from a URL (no body). Returns: status, headers.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "URL to inspect (http/https only)"},
            },
            "required": ["url"],
        },
    },
    {
        "name": "fetch_headers",
        "description": "Parse and return response headers as a dict. Same as fetch_head but returns formatted headers.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "URL to inspect (http/https only)"},
            },
            "required": ["url"],
        },
    },
]


def _setup():
    """Init logging once — central rotating rail instead of a raw FileHandler."""
    global _LOG
    if _LOG is not None:
        return
    from toolrecall.logging_setup import get_logger

    _LOG = get_logger("fetch")


def _no_redirects() -> HTTPRedirectHandler:
    """Handler that refuses to follow redirects (SSRF guard, F14).

    urllib follows 301/302/303/307 transparently by default; a redirect
    could bounce a validated public URL onto an internal address that was
    never validated. The HTTPError is surfaced to the caller instead —
    the MCP tool result shows the redirect, the agent can re-request the
    target explicitly (it will be validated like any other URL).
    """
    return _NoRedirects()


class _NoRedirects(HTTPRedirectHandler):
    """Redirect handler that never redirects: returns None so urllib raises
    HTTPError for 3xx, surfacing the redirect target to the caller."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _validate_url(url: str) -> str | None:
    """Validate URL. Returns error message or None on success.

    Blocks SSRF-prone destinations (F14): loopback, link-local (169.254/16 —
    includes the cloud metadata endpoint), RFC 1918/CGNAT private ranges,
    IPv6 unique-local, IPv4-mapped IPv6, and decimal/octal/hex IP spellings
    (urlsplit normalizes none of them, so resolve the host explicitly).
    The request pins redirects to the initially validated host via
    _NoRedirects — a 302 to an internal address is not followed.
    """
    if not url or not url.strip():
        return "URL is required"
    parsed = urlparse(url)
    if parsed.scheme not in ALLOWED_SCHEMES:
        return f"Unsupported scheme '{parsed.scheme}'. Only http and https are allowed."
    if not parsed.hostname:
        return "Invalid URL: missing hostname"
    # Credentials in the netloc would make the blocked-host check below
    # apply to a userinfo string, not the real destination host.
    if parsed.username is not None or parsed.password is not None:
        return "Invalid URL: credentials in the URL are not allowed"
    host = parsed.hostname.rstrip(".")
    try:
        infos = socket.getaddrinfo(
            host,
            parsed.port or (443 if parsed.scheme == "https" else 80),
            type=socket.SOCK_STREAM,
        )
    except socket.gaierror as e:
        return f"Invalid URL: cannot resolve host ({e})"
    for family, _type, _proto, _canonname, sockaddr in infos:
        addr = ipaddress.ip_address(sockaddr[0])
        if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
            addr = addr.ipv4_mapped  # ::ffff:10.0.0.1 is IPv4 traffic
        ip = addr
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
            or ip in _CGNAT
        ):
            return f"Blocked URL: {host} resolves to a non-public address ({ip}) — SSRF protection"
    return None


def _do_fetch(url: str, method: str = "GET", max_bytes: int = MAX_CONTENT_BYTES) -> dict:
    """Execute HTTP request. Returns result dict."""
    assert _LOG is not None
    max_bytes = min(max_bytes, MAX_CONTENT_BYTES)

    _LOG.info(f"{method} {url}")
    req = Request(url, method=method)
    req.add_header("User-Agent", "toolrecall-fetch-mcp/0.1")
    req.add_header("Accept", "text/html,application/xhtml+xml,application/xml,text/plain,*/*")

    # SSRF guard (F14): never follow redirects — the redirect target was
    # never validated and could be an internal address. The resulting
    # HTTPError (3xx) is surfaced to the caller below.
    opener = build_opener(_no_redirects())
    try:
        with opener.open(req, timeout=TIMEOUT_SECONDS) as resp:
            status = resp.status
            headers = dict(resp.headers)
            content_type = resp.headers.get("Content-Type", "").split(";")[0].strip()
            encoding = resp.headers.get("Content-Encoding", "")

            if method == "HEAD":
                return {
                    "status": status,
                    "headers": headers,
                    "content_type": content_type,
                }

            # Read content (truncated)
            raw = resp.read(max_bytes)
            truncated = len(raw) >= max_bytes

            # Try to decode as text
            try:
                content = raw.decode("utf-8")
            except UnicodeDecodeError:
                try:
                    content = raw.decode("latin-1")
                except Exception:
                    content = f"[Binary content: {len(raw)} bytes, content-type: {content_type}]"

            return {
                "status": status,
                "headers": headers,
                "content": content,
                "content_type": content_type,
                "encoding": encoding,
                "bytes_read": len(raw),
                "truncated": truncated,
            }

    except HTTPError as e:
        _LOG.warning(f"HTTP {e.code} for {url}")
        return {
            "status": e.code,
            "error": str(e.reason),
            "headers": dict(e.headers) if e.headers else {},
        }
    except URLError as e:
        _LOG.error(f"URL error for {url}: {e.reason}")
        return {"status": 0, "error": f"URL error: {e.reason}"}
    except Exception as e:
        _LOG.error(f"Fetch failed for {url}: {e}")
        return {"status": 0, "error": str(e)}


def _handle(method: str, params: dict) -> dict | None:
    """Dispatch MCP tool calls."""
    if method == "fetch_url":
        url = params.get("url", "")
        err = _validate_url(url)
        if err:
            return {"error": err}
        max_bytes = params.get("max_bytes", MAX_CONTENT_BYTES)
        if isinstance(max_bytes, int) and max_bytes > 0:
            pass
        else:
            max_bytes = MAX_CONTENT_BYTES
        result = _do_fetch(url, "GET", max_bytes)
        return result

    elif method == "fetch_head":
        url = params.get("url", "")
        err = _validate_url(url)
        if err:
            return {"error": err}
        return _do_fetch(url, "HEAD")

    elif method == "fetch_headers":
        url = params.get("url", "")
        err = _validate_url(url)
        if err:
            return {"error": err}
        result = _do_fetch(url, "HEAD")
        if "headers" in result:
            result["headers_formatted"] = "\n".join(
                f"{k}: {v}" for k, v in result["headers"].items()
            )
        return result

    return None


def main():
    """MCP stdio loop."""
    _setup()
    assert _LOG is not None
    sys.stderr.write("ToolRecall Fetch MCP Server (Python stdlib, zero deps)\n")
    sys.stderr.flush()
    _LOG.info("Fetch MCP server started")

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue

        rid = req.get("id", 0)
        method = req.get("method", "")
        params = req.get("params", {})

        resp = {"jsonrpc": "2.0", "id": rid}

        if method == "initialize":
            resp["result"] = {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "toolrecall-fetch", "version": "0.1.0"},
            }
        elif method == "tools/list":
            resp["result"] = {"tools": TOOLS}
        elif method == "tools/call":
            result = _handle(params.get("name", ""), params.get("arguments", {}))
            if result is None:
                resp["error"] = {"code": -32601, "message": "Unknown tool"}
            elif "error" in result:
                resp["error"] = {"code": -32000, "message": result["error"]}
            else:
                resp["result"] = {
                    "content": [{"type": "text", "text": json.dumps(result, indent=2)}]
                }
        elif method in ("notifications/initialized", "close"):
            continue
        else:
            resp["error"] = {"code": -32601, "message": f"Unknown method: {method}"}

        sys.stdout.write(json.dumps(resp) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
