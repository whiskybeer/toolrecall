"""ToolRecall Docs MCP Server — stdlib-only FTS5 knowledge search.
Exposes the in-core knowledge base (toolrecall.docs, SQLite FTS5 + BM25)
as a standalone MCP server. Zero external dependencies — same engine the
`toolrecall mcp` bridge exposes natively, for agents that instead connect
through the multiplexer.
"""

import json
import sys

from toolrecall.docs import docs_get_page, docs_search

TOOLS = [
    {
        "name": "docs_search",
        "description": "Full-text search across indexed documents (FTS5+BM25).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search terms"},
                "source": {
                    "type": "string",
                    "description": "Optional namespace filter (e.g. 'hermes-docs')",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "docs_get_page",
        "description": "Retrieve an indexed document page by source and path.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "source": {"type": "string", "description": "Document source/namespace"},
                "path": {"type": "string", "description": "Document path"},
            },
            "required": ["source", "path"],
        },
    },
]


def _handle(method, params):
    # docs.py functions return user-facing strings directly — no extra
    # {"result": ...} wrapper, or agents would receive a JSON-encoded dict
    # where the bridge/other servers return plain text.
    if method == "docs_search":
        return docs_search(params.get("query", ""), params.get("source"))
    if method == "docs_get_page":
        return docs_get_page(params.get("path", ""), params.get("source", ""))
    return None


def main():
    sys.stderr.write("ToolRecall Docs MCP Server (stdlib FTS5, zero deps)\n")
    sys.stderr.flush()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        rid, method, params = req.get("id", 0), req.get("method", ""), req.get("params", {})
        resp = {"jsonrpc": "2.0", "id": rid}
        if method == "initialize":
            resp["result"] = {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "toolrecall-docs", "version": "0.1.0"},
            }
        elif method == "tools/list":
            resp["result"] = {"tools": TOOLS}
        elif method == "tools/call":
            result = _handle(params.get("name", ""), params.get("arguments", {}))
            if result is None:
                resp["error"] = {"code": -32601, "message": "Unknown tool"}
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
