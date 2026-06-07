"""ToolRecall MCP Bridge — stdio ↔ UDS Protocol Adapter.

The MCP Bridge is a thin adapter: it reads MCP JSON-RPC from stdin,
translates the requests into UDS calls to the ToolRecall Daemon and writes
the responses back to stdout.

It has NO caching logic of its own, NO SQLite, NO In-Memory LRU.
Everything runs through the Daemon. This makes it:
  - Slim (~100 LOC instead of 540)
  - Fast to start (~5ms instead of 200ms)
  - Secure (Security lies in the Daemon, not in the bridge)

Usage:
    toolrecall mcp              # Start Bridge (requires Daemon)
    toolrecall mcp-legacy       # Legacy: standalone MCP Server (no Daemon)

Requires a running ToolRecall Daemon:
    toolrecall daemon &         # Start once
    toolrecall mcp              # Run bridge
"""

import json
import os
import socket
import struct
import sys
from pathlib import Path

from toolrecall.client import UDSClient


# ─── MCP Tool Definitions ────────────────────────────────

TOOL_DEFINITIONS = [
    {
        "name": "cached_read",
        "description": "Read a file with hybrid In-Memory + SQLite cache. "
                       "Cached until file modification time (mtime) changes.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path to read"}
            },
            "required": ["path"]
        }
    },
    {
        "name": "cached_terminal",
        "description": "Run a terminal command with TTL-based caching. "
                       "⚠ Requires mcp.allow_terminal=true in config.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Shell command"},
                "ttl": {"type": "integer", "description": "Cache TTL in seconds (0=bypass)"}
            },
            "required": ["command"]
        }
    },
    {
        "name": "cached_skill",
        "description": "View a Hermes skill with caching.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Skill name"}
            },
            "required": ["name"]
        }
    },
    {
        "name": "docs_search",
        "description": "Full-text search across indexed documents (FTS5+BM25). "
                       "No embeddings, no GPU, no API calls.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search terms"},
                "source": {"type": "string", "description": "Optional namespace filter"}
            },
            "required": ["query"]
        }
    },
    {
        "name": "docs_get_page",
        "description": "Retrieve a specific indexed page by source and path.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "source": {"type": "string", "description": "Document source/namespace"},
                "path": {"type": "string", "description": "Document path"}
            },
            "required": ["source", "path"]
        }
    },
    {
        "name": "cache_status",
        "description": "Show cache statistics (hits, misses, tokens saved).",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "required": []
        }
    },
    {
        "name": "cache_invalidate",
        "description": "Clear all ToolRecall caches. "
                       "⚠ Requires mcp.allow_invalidate=true in config.",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "required": []
        }
    },
    {
        "name": "mcp_call",
        "description": "Call a tool on a multiplexed MCP server (github, time, fetch, etc.). "
                       "The daemon manages persistent subprocesses for all MCP servers. "
                       "Use mcp_list_servers first to discover available servers and tools. "
                       "⚠ Requires mcp_multiplex.enabled=true in config.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "server": {"type": "string", "description": "MCP server name (e.g. 'github', 'time', 'fetch')"},
                "tool": {"type": "string", "description": "Tool name on that server (e.g. 'list_issues')"},
                "arguments": {"type": "object", "description": "Tool arguments dict"},
                "bypass_cache": {"type": "boolean", "description": "Skip cache and force fresh call"}
            },
            "required": ["server", "tool"]
        }
    },
    {
        "name": "mcp_list_servers",
        "description": "List available multiplexed MCP servers and their tools. "
                       "Returns name, running status, and tool names for each server.",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "required": []
        }
    },
]

CMD_TO_MCP = {
    "cached_read": "cached_read",
    "cached_terminal": "cached_terminal",
    "cached_skill": "cached_skill",
    "docs_search": "docs_search",
    "docs_get_page": "docs_get_page",
    "cache_status": "cache_status",
    "cache_invalidate": "cache_invalidate",
    "mcp_call": "mcp_call",
    "mcp_list_servers": "mcp_list_servers",
}


# ─── MCP Bridge ───────────────────────────────────────────

class MCPBridge:
    """Liest MCP JSON-RPC von stdin, leitet an Daemon weiter, schreibt auf stdout."""

    def __init__(self, socket_path: str = None):
        self.client = UDSClient(socket_path)

    def _uds_request(self, cmd: str, **kwargs) -> dict:
        """Send a request to the daemon and return parsed response."""
        payload = {"cmd": cmd, **kwargs}
        return self.client._send(payload)

    def _format_result(self, result) -> str:
        """Format a result for MCP text content."""
        if isinstance(result, dict):
            return json.dumps(result, indent=2)
        return str(result)

    def handle_request(self, req: dict) -> dict:
        """Handle one JSON-RPC request."""
        method = req.get("method", "")
        req_id = req.get("id")
        params = req.get("params", {})

        if method == "initialize":
            return self._handle_initialize(req_id)
        elif method == "tools/list":
            return self._handle_tools_list(req_id)
        elif method == "tools/call":
            return self._handle_tool_call(req_id, params)
        elif method == "notifications/initialized":
            return None
        elif method == "close":
            return None
        else:
            return self._error(req_id, -32601, f"Method not found: {method}")

    def _handle_initialize(self, req_id):
        # Ping daemon to get security info
        info = self._uds_request("ping")
        security = {
            "allowed_paths": info.get("allowed_paths", []),
            "allow_terminal": info.get("allow_terminal", False),
            "allow_invalidate": info.get("allow_invalidate", False),
        }
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {
                    "name": "ToolRecall (Bridge)",
                    "version": "0.2.0",
                    "security": security,
                },
                "instructions": (
                    "ToolRecall — Tool-Output Cache for LLM Agents (MCP Bridge).\n\n"
                    "This bridge connects to the ToolRecall daemon. "
                    "5 tools always available, 2 opt-in via config.\n"
                    f"  cached_read: path-allowlisted\n"
                    f"  cached_terminal: {'ENABLED' if security['allow_terminal'] else 'DISABLED'}\n"
                    f"  cache_invalidate: {'ENABLED' if security['allow_invalidate'] else 'DISABLED'}\n\n"
                    "Start daemon: toolrecall daemon"
                ),
            }
        }

    def _handle_tools_list(self, req_id):
        # Ask daemon which tools are actually available (gates)
        info = self._uds_request("ping")
        allow_terminal = info.get("allow_terminal", False)
        allow_invalidate = info.get("allow_invalidate", False)
        multiplex_enabled = info.get("multiplex_enabled", False)

        tools = []
        for tdef in TOOL_DEFINITIONS:
            name = tdef["name"]
            if name == "cached_terminal" and not allow_terminal:
                continue
            if name == "cache_invalidate" and not allow_invalidate:
                continue
            if name in ("mcp_call", "mcp_list_servers") and not multiplex_enabled:
                continue
            tools.append(tdef)

        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {"tools": tools}
        }

    def _handle_tool_call(self, req_id, params):
        tool_name = params.get("name", "")
        arguments = params.get("arguments", {})

        uds_cmd = CMD_TO_MCP.get(tool_name)
        if not uds_cmd:
            return self._error(req_id, -32601, f"Unknown tool: {tool_name}")

        try:
            # mcp_call: server, tool, arguments (dict) are top-level params
            if tool_name == "mcp_call":
                server = arguments.get("server", "")
                tool = arguments.get("tool", "")
                tool_args = arguments.get("arguments", {})
                bypass = arguments.get("bypass_cache", False)
                payload = {
                    "cmd": "mcp_call",
                    "server": server,
                    "tool": tool,
                    "arguments": tool_args,
                }
                if bypass:
                    payload["ttl"] = 0
                resp = self.client._send(payload)
            elif tool_name == "mcp_list_servers":
                resp = self.client._send({"cmd": "mcp_list_servers"})
            else:
                resp = self._uds_request(uds_cmd, **arguments)

            if "error" in resp:
                return self._error(req_id, -32603, resp["error"])

            # Extract result for presentation
            content = resp.get("result", resp)
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": self._format_result(content)}]
                }
            }
        except Exception as e:
            return self._error(req_id, -32603, str(e))

    def _error(self, req_id, code, message):
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": code, "message": message}
        }


# ─── Entry Point ──────────────────────────────────────────

def main():
    """Start the MCP Bridge (stdio → Daemon ↔ UDS)."""
    import sys

    bridge = MCPBridge()

    # Try to ping daemon
    ping = bridge._uds_request("ping")
    if ping.get("error") == "daemon_unavailable":
        print("❌ ToolRecall daemon is not running.", file=sys.stderr)
        print("   Run: toolrecall daemon &", file=sys.stderr)
        print("   Or:  toolrecall mcp --direct   (legacy standalone)", file=sys.stderr)
        sys.exit(1)

    print(f"ToolRecall MCP Bridge v0.2.0", file=sys.stderr)
    print(f"  Connected to daemon", file=sys.stderr)
    term = ping.get("allow_terminal", False)
    inv = ping.get("allow_invalidate", False)
    paths = ping.get("allowed_paths", [])
    print(f"  cached_read path allowlist: {', '.join(paths) if paths else 'ALL (DANGEROUS)'}", file=sys.stderr)
    print(f"  cached_terminal: {'ENABLED' if term else 'DISABLED'}", file=sys.stderr)
    print(f"  cache_invalidate: {'ENABLED' if inv else 'DISABLED'}", file=sys.stderr)
    print(file=sys.stderr)

    # Read JSON-RPC from stdin line by line
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            continue
        response = bridge.handle_request(request)
        if response is not None:
            sys.stdout.write(json.dumps(response) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
