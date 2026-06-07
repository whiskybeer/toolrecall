"""ToolRecall HTTP Proxy — standalone mode for agents that can't import Python.

Architecture:
==============
ToolRecall has THREE operational modes, each serving a different agent type:

┌─────────────────────────────────────────────────────────────┐
│                    ToolRecall Architecture                   │
├─────────────────────────────────────────────────────────────┤
│                                                              │
│  ┌──────────────┐    ┌──────────────────┐    ┌──────────┐  │
│  │ Python Import │    │  HTTP Proxy      │    │  CLI     │  │
│  │ (direct)      │    │  (toolrecall     │    │ (CI/CD)  │  │
│  │               │    │   serve)         │    │          │  │
│  │ Hermes        │    │  ┌────────────┐  │    │ status   │  │
│  │ Claude Code*  │    │  │ Port 8511  │  │    │ stats    │  │
│  │ Codex*        │    │  │            │  │    │ index    │  │
│  │ Any Python    │    │  │ /cached_*  │  │    │ nginx    │  │
│  │ agent         │    │  │ /docs_*    │  │    └──────────┘  │
│  └──────┬───────┘    │  │ /health    │  │                   │
│         │            │  └─────┬──────┘  │                   │
│         ▼            │        │         │                   │
│  ┌──────────────────────────────────────────┐               │
│  │  ToolRecall Cache Core (SQLite FTS5)     │               │
│  │  ┌──────┐ ┌──────┐ ┌──────┐ ┌───────┐  │               │
│  │  │File  │ │Term  │ │Skill │ │Script/│  │               │
│  │  │Cache │ │Cache │ │Cache │ │Code   │  │               │
│  │  └──────┘ └──────┘ └──────┘ └───────┘  │               │
│  └──────────────────────────────────────────┘               │
│              │                                               │
│              ▼                                               │
│  ┌──────────────────┐    ┌────────────────┐                 │
│  │ ~/.toolrecall/   │    │ Nginx (opt.)   │                 │
│  │ cache.db         │    │ SSL terminator │                 │
│  │ knowledge.db     │    │ Port 443       │                 │
│  └──────────────────┘    └────────────────┘                 │
│                                                              │
│  * = via HTTP proxy only (no Python import possible)         │
└─────────────────────────────────────────────────────────────┘

Port 8511:
==========
The HTTP proxy listens on port 8511 by default (configurable via [proxy].port).
This port was chosen because:
  1. Not in the system-reserved range (< 1024) — no root needed
  2. Not conflicting with common services (22, 80, 443, 3000, 8080, etc.)
  3. Easy to remember: "85" = TR (ToolRecall) in phone-keypad logic
  4. The proxy is a PLAIN HTTP server (Python stdlib http.server) —
     NO external dependencies needed

Nginx is recommended IN FRONT of the proxy for SSL termination + auth.
The proxy itself does NOT handle SSL — it's intentionally kept dependency-free.

Endpoint Reference:
====================
GET /cached_read?path=/file       → cached file read (mtime-based)
GET /cached_terminal?cmd=...&ttl= → cached terminal (TTL-based)
GET /cached_skill?name=...        → cached skill view (mtime-based)
GET /docs_search?query=...        → full-text search (BM25)
GET /health                       → {"status": "ok"}
"""

import http.server
import json
import urllib.parse
from toolrecall.cache import cached_read, cached_skill, cached_terminal
from toolrecall.docs import docs_search
from toolrecall.config import load_config

VERSION = "0.1.0"


class ToolRecallHandler(http.server.BaseHTTPRequestHandler):
    """HTTP request handler for ToolRecall proxy endpoints.

    All responses are JSON. No external dependencies required.
    """

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
                    result = cached_read(p)

            elif path == "/cached_skill":
                s = q.get("name", "")
                if not s:
                    result = {"error": "Missing 'name' query parameter"}
                else:
                    result = cached_skill(s)

            elif path == "/cached_terminal":
                c = q.get("cmd", "")
                if not c:
                    result = {"error": "Missing 'cmd' query parameter"}
                else:
                    ttl = int(q.get("ttl", "0")) or None
                    result = cached_terminal(c, ttl)

            elif path == "/docs_search":
                query = q.get("query", "")
                if not query:
                    result = {"error": "Missing 'query' query parameter"}
                else:
                    src = q.get("source", None)
                    result = {"output": docs_search(query, src)}

            elif path == "/health":
                result = {"status": "ok", "version": VERSION}

            else:
                result = {"error": f"Unknown endpoint: {path}"}
                self.send_response(404)

            self.send_response(200)
        except Exception as e:
            result = {"error": str(e)}
            self.send_response(500)

        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(result).encode())

    def log_message(self, fmt, *args):
        """Suppress default request logging (too noisy)."""
        pass


def run_server(bind: str = "[IP_ADDRESS]", port: int = 8511):
    """Start the ToolRecall HTTP proxy server.

    Args:
        bind: Host to bind to (default: [IP_ADDRESS] = all interfaces)
        port: Port to listen on (default: 8511)
    """
    server = http.server.HTTPServer((bind, port), ToolRecallHandler)
    print(f"ToolRecall HTTP proxy running on http://{bind}:{port}")
    print(f"Endpoints:")
    print(f"  GET /cached_read?path=/path/to/file")
    print(f"  GET /cached_skill?name=skill-name")
    print(f"  GET /cached_terminal?cmd=<command>&ttl=<seconds>")
    print(f"  GET /docs_search?query=<search terms>")
    print(f"  GET /health")
    print()
    print("Recommended: put nginx in front for SSL + auth.")
    print("  toolrecall nginx  →  generates nginx config")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
