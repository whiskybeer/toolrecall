"""ToolRecall MCP Server — Expose cache/search as MCP tools.

SECURITY MODEL
==============
The MCP server exposes ToolRecall cache functions as MCP tools. This gives
any connected MCP client (Hermes, Claude Code, Cursor, etc.) access to:

  SAFE (always available):
    - docs_search, docs_get_page   → read-only FTS5 knowledge base
    - cached_skill                 → read-only skill content
    - cache_status                 → read-only statistics

  CONDITIONAL (config-gated):
    - cached_read                  → path-allowlisted file reads
    - cached_terminal              → shell execution (OFF by default!)
    - cache_invalidate             → destructive (OFF by default!)

Why isn't cached_terminal enabled by default?
  - MCP bypasses all local security guards (prompt confirmation, tool allowlist)
  - An agent connected to the MCP server can call cached_terminal("rm -rf /")
  - The first call ALWAYS executes the command (TTL cache only helps on repeat)
  - Even with the tool allowlist (read-only cmds), confidential data could be exfiltrated

RECOMMENDED SETUP
=================
  Level 1 (BEST): Python import via hermes_init.py
    → Full API, zero network exposure, no subprocess overhead
    → Register in Hermes: agent.init_scripts = ["~/.toolrecall/hermes_init.py"]

  Level 2: Hermes MCP client (mcp_servers config)
    → stdio-local, no network, auto-injected tools
    → Only Hermes itself has access
    → Add to ~/.hermes/config.yaml:
        mcp_servers:
          toolrecall:
            command: "uv"
            args: ["run", "python", "-m", "toolrecall.mcp_server"]
            timeout: 30

  Level 3: HTTP proxy (toolrecall serve)
    → For HTTP-speaking agents (Claude Code, Cursor)
    → Network-exposed — put nginx+auth in front

Usage:
    toolrecall mcp
    # or: python -m toolrecall.mcp_server
"""

import sys
import json
import os
from pathlib import Path

from toolrecall.cache import (
    cached_read, cached_skill, cached_terminal,
    invalidate_all, get_stats,
)
from toolrecall.docs import docs_search as _docs_search, docs_get_page as _docs_get_page
from toolrecall.config import load_config

# Try FastMCP; fall back to minimal stdio
try:
    from mcp.server.fastmcp import FastMCP
    HAS_FASTMCP = True
except ImportError:
    HAS_FASTMCP = False


# ─── Security helpers ──────────────────────────────────────

def _is_path_allowed(path: str, allowed_paths: list) -> bool:
    """Check if a given path is within one of the allowed directories.
    
    If allowed_paths is empty, ALL paths are permitted (DANGEROUS — fallback only).
    """
    if not allowed_paths:
        return True  # empty = everything allowed (legacy/compat)
    abs_path = os.path.abspath(os.path.expanduser(path))
    for allowed in allowed_paths:
        allowed_abs = os.path.abspath(os.path.expanduser(allowed))
        if abs_path == allowed_abs or abs_path.startswith(allowed_abs + os.sep):
            return True
    return False


def _format_result(result) -> str:
    if isinstance(result, dict):
        return json.dumps(result, indent=2)
    return str(result)


def _get_config():
    """Load config once. Cached globally."""
    return load_config()


# ─── FastMCP Implementation ────────────────────────────────

def create_fastmcp_server() -> FastMCP:
    """Create and configure the ToolRecall FastMCP server (security-gated)."""
    cfg = _get_config()
    allowed_paths = cfg.mcp_allowed_paths
    allow_terminal = cfg.mcp_allow_terminal
    allow_invalidate = cfg.mcp_allow_invalidate

    active_tools = []
    if allowed_paths:
        active_tools.append("   cached_read  →  path-allowlisted file reads")
    else:
        active_tools.append("   cached_read  →  ⚠️ UNRESTRICTED file reads (all paths)")
    active_tools.append("   cached_skill →  Hermes skill content")
    active_tools.append("   docs_search  →  FTS5 full-text search")
    active_tools.append("   docs_get_page→  indexed page retrieval")
    active_tools.append("   cache_status →  cache statistics")
    if allow_terminal:
        active_tools.append("   cached_terminal →  ⚠️ SHELL EXECUTION (opt-in)")
    if allow_invalidate:
        active_tools.append("   cache_invalidate →  cache clearing (opt-in)")

    sec_note = []
    if allowed_paths:
        sec_note.append(f"cached_read restricted to: {', '.join(allowed_paths)}")
    if not allow_terminal:
        sec_note.append("cached_terminal DISABLED (set mcp.allow_terminal=true to enable)")
    if not allow_invalidate:
        sec_note.append("cache_invalidate DISABLED (set mcp.allow_invalidate=true to enable)")

    mcp = FastMCP(
        "ToolRecall",
        instructions=f"""\
ToolRecall — Tool-Output Cache for LLM Agents (MCP Server).

SAFE tools (always available):
{chr(10).join(active_tools)}

SECURITY:
{chr(10).join(sec_note)}

RECOMMENDATION:
  Level 1 (best): Python import via hermes_init.py
  Level 2: Hermes mcp_servers config
  Level 3: HTTP proxy (toolrecall serve)
"""
    )

    # ─── cached_read (path-allowlisted) ──────────────────
    read_desc = "Read a file with hybrid In-Memory + SQLite cache."
    if allowed_paths:
        read_desc += f" Restricted to: {', '.join(allowed_paths)}."

    @mcp.tool(name="cached_read", description=read_desc)
    def tool_cached_read(path: str) -> str:
        """Read a file via ToolRecall cache (path-allowlist enforced).

        Args:
            path: Absolute or relative path to the file
        """
        if not _is_path_allowed(path, allowed_paths):
            return json.dumps({
                "error": f"Path not allowed: {path}",
                "allowed_paths": list(allowed_paths) if allowed_paths else ["ALL"]
            }, indent=2)
        result = cached_read(path)
        return _format_result(result)

    # ─── cached_skill (safe) ─────────────────────────────
    @mcp.tool(
        name="cached_skill",
        description="View a Hermes skill with caching. "
                    "Cached until any file in the skill directory changes."
    )
    def tool_cached_skill(name: str) -> str:
        """View a skill via ToolRecall cache.

        Args:
            name: Skill name (e.g. 'tool-recall', 'native-mcp')
        """
        result = cached_skill(name)
        if isinstance(result, dict):
            content = result.get("content", str(result))
            return str(content) if content else json.dumps(result, indent=2)
        return str(result)

    # ─── docs_search (safe) ──────────────────────────────
    @mcp.tool(
        name="docs_search",
        description="Full-text search across indexed documents (skills, projects, code). "
                    "Uses SQLite FTS5 with BM25 ranking and Porter stemming. "
                    "No embeddings, no GPU, no API calls needed."
    )
    def tool_docs_search(query: str, source: str = None) -> str:
        """Search indexed knowledge base.

        Args:
            query: Search terms (Porter stemming included automatically)
            source: Optional namespace filter (e.g. 'hermes', 'ki-game')
        """
        result = _docs_search(query, source=source)
        return result if result else "No results found."

    # ─── docs_get_page (safe) ────────────────────────────
    @mcp.tool(
        name="docs_get_page",
        description="Retrieve a specific indexed page from the knowledge base "
                    "by source and path."
    )
    def tool_docs_get_page(source: str, path: str) -> str:
        """Get a specific indexed page.

        Args:
            source: Document source/namespace (e.g. 'hermes', 'ki-game')
            path: Document path within that namespace
        """
        result = _docs_get_page(source, path)
        return _format_result(result)

    # ─── cache_status (safe) ─────────────────────────────
    @mcp.tool(
        name="cache_status",
        description="Show ToolRecall cache statistics: "
                    "hits, misses, hit rates, tokens saved per cache type."
    )
    def tool_cache_status() -> str:
        """Get cache statistics."""
        stats = get_stats()
        lines = ["ToolRecall Cache Status", "=" * 40]
        for k, v in stats.items():
            if isinstance(v, dict):
                lines.append(f"\n{k}:")
                for sk, sv in v.items():
                    lines.append(f"  {sk}: {sv}")
            else:
                lines.append(f"{k}: {v}")
        return "\n".join(lines)

    # ─── cached_terminal (opt-in!) ───────────────────────
    if allow_terminal:
        @mcp.tool(
            name="cached_terminal",
            description="⚠️ Run a terminal command with TTL-based caching. "
                        "ENABLED via mcp.allow_terminal=true — SECURITY RISK. "
                        "First call always executes the command."
        )
        def tool_cached_terminal(command: str, ttl: int = None) -> str:
            """Run a terminal command via ToolRecall cache (opt-in).

            Args:
                command: Shell command to execute
                ttl: Cache TTL in seconds (0 = bypass, None = default)
            """
            result = cached_terminal(command, ttl=ttl)
            return _format_result(result)

    # ─── cache_invalidate (opt-in!) ──────────────────────
    if allow_invalidate:
        @mcp.tool(
            name="cache_invalidate",
            description="Clear all ToolRecall caches. "
                        "ENABLED via mcp.allow_invalidate=true."
        )
        def tool_cache_invalidate() -> str:
            """Invalidate all caches."""
            before = get_stats()
            invalidate_all()
            after = get_stats()
            return (
                "✅ All ToolRecall caches cleared.\n"
                f"Before: {json.dumps(before, indent=2)}\n"
                f"After: {json.dumps(after, indent=2)}"
            )

    return mcp


# ─── Manual stdio server (fallback) ────────────────────────

def _build_tool_list(cfg):
    """Build MCP tool list based on config-gated security."""
    allowed_paths = cfg.mcp_allowed_paths
    allow_terminal = cfg.mcp_allow_terminal
    allow_invalidate = cfg.mcp_allow_invalidate

    tools = []

    # cached_read (path-allowlisted)
    read_desc = "Read a file with hybrid In-Memory + SQLite cache"
    if allowed_paths:
        read_desc += f" (restricted to {', '.join(allowed_paths)})"
    tools.append({
        "name": "cached_read",
        "description": read_desc,
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path to read"}
            },
            "required": ["path"]
        }
    })

    # cached_skill
    tools.append({
        "name": "cached_skill",
        "description": "View a Hermes skill with caching",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Skill name"}
            },
            "required": ["name"]
        }
    })

    # docs_search
    tools.append({
        "name": "docs_search",
        "description": "Full-text search across indexed documents (FTS5+BM25)",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search terms"},
                "source": {"type": "string",
                           "description": "Optional namespace filter"}
            },
            "required": ["query"]
        }
    })

    # docs_get_page
    tools.append({
        "name": "docs_get_page",
        "description": "Retrieve a specific indexed page by source and path",
        "inputSchema": {
            "type": "object",
            "properties": {
                "source": {"type": "string",
                           "description": "Document source/namespace"},
                "path": {"type": "string", "description": "Document path"}
            },
            "required": ["source", "path"]
        }
    })

    # cache_status
    tools.append({
        "name": "cache_status",
        "description": "Show cache statistics (hits, misses, tokens saved)",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "required": []
        }
    })

    # cached_terminal (opt-in)
    if allow_terminal:
        tools.append({
            "name": "cached_terminal",
            "description": "⚠️ Run a terminal command with TTL cache (opt-in)",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "command": {"type": "string",
                                "description": "Shell command"},
                    "ttl": {"type": "integer",
                            "description": "Cache TTL (0=bypass)"}
                },
                "required": ["command"]
            }
        })

    # cache_invalidate (opt-in)
    if allow_invalidate:
        tools.append({
            "name": "cache_invalidate",
            "description": "Clear all ToolRecall caches",
            "inputSchema": {
                "type": "object",
                "properties": {},
                "required": []
            }
        })

    return tools


def run_stdio_fallback():
    """Ultra-minimal MCP server using raw stdio JSON-RPC.

    Used when the `mcp` package (FastMCP) is not installed.
    Supports: initialize, tools/list, tools/call.
    """
    cfg = _get_config()
    allowed_paths = cfg.mcp_allowed_paths
    allow_terminal = cfg.mcp_allow_terminal
    allow_invalidate = cfg.mcp_allow_invalidate

    # Security status banner (same as FastMCP main())
    print(f"ToolRecall MCP Server (stdio fallback)", file=sys.stderr)
    print(f"  cached_read path allowlist: "
          f"{', '.join(allowed_paths) if allowed_paths else 'ALL PATHS (DANGEROUS)'}",
          file=sys.stderr)
    print(f"  cached_terminal: {'ENABLED' if allow_terminal else 'DISABLED (default)'}",
          file=sys.stderr)
    print(f"  cache_invalidate: {'ENABLED' if allow_invalidate else 'DISABLED (default)'}",
          file=sys.stderr)
    print(file=sys.stderr)

    TOOLS = _build_tool_list(cfg)

    def handle_safe_tool(tool_name: str, arguments: dict):
        """Route a tool call with security checks."""
        if tool_name == "cached_read":
            path = arguments["path"]
            if not _is_path_allowed(path, allowed_paths):
                return {"error": f"Path not allowed: {path}",
                        "allowed_paths": list(allowed_paths) if allowed_paths else ["ALL"]}
            return cached_read(path)
        elif tool_name == "cached_skill":
            return cached_skill(arguments["name"])
        elif tool_name == "docs_search":
            return _docs_search(arguments["query"], source=arguments.get("source"))
        elif tool_name == "docs_get_page":
            return _docs_get_page(arguments["source"], arguments["path"])
        elif tool_name == "cache_status":
            return get_stats()
        elif tool_name == "cached_terminal":
            if not allow_terminal:
                return {"error": "cached_terminal is disabled. "
                                 "Set mcp.allow_terminal=true in config."}
            return cached_terminal(arguments["command"], ttl=arguments.get("ttl"))
        elif tool_name == "cache_invalidate":
            if not allow_invalidate:
                return {"error": "cache_invalidate is disabled. "
                                 "Set mcp.allow_invalidate=true in config."}
            invalidate_all()
            return {"status": "ok", "message": "All caches cleared"}
        return None

    def handle_request(req: dict) -> dict:
        method = req.get("method", "")
        req_id = req.get("id", 0)
        params = req.get("params", {})

        if method == "initialize":
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {
                        "name": "ToolRecall",
                        "version": "0.2.0",
                        "security": {
                            "allowed_paths": list(allowed_paths) if allowed_paths else ["ALL"],
                            "allow_terminal": allow_terminal,
                            "allow_invalidate": allow_invalidate,
                        }
                    }
                }
            }
        elif method == "tools/list":
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {"tools": TOOLS}
            }
        elif method == "tools/call":
            tool_name = params.get("name", "")
            arguments = params.get("arguments", {})
            try:
                result = handle_safe_tool(tool_name, arguments)
                if result is None:
                    return {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "error": {"code": -32601,
                                  "message": f"Unknown tool: {tool_name}"}
                    }
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "content": [{"type": "text",
                                     "text": _format_result(result)}]
                    }
                }
            except Exception as e:
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": -32603, "message": str(e)}
                }
        elif method in ("notifications/initialized", "close"):
            return None
        else:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32601,
                          "message": f"Method not found: {method}"}
            }

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            continue
        response = handle_request(request)
        if response is not None:
            sys.stdout.write(json.dumps(response) + "\n")
            sys.stdout.flush()


# ─── Entry Point ──────────────────────────────────────────────

def main():
    """Start the ToolRecall MCP server (with security gates)."""
    cfg = _get_config()
    allowed = cfg.mcp_allowed_paths
    term = cfg.mcp_allow_terminal
    inv = cfg.mcp_allow_invalidate

    print(f"ToolRecall MCP Server v0.2.0", file=sys.stderr)
    print(f"  cached_read path allowlist: "
          f"{', '.join(allowed) if allowed else 'ALL PATHS (DANGEROUS)'}",
          file=sys.stderr)
    print(f"  cached_terminal: {'ENABLED' if term else 'DISABLED (default)'}",
          file=sys.stderr)
    print(f"  cache_invalidate: {'ENABLED' if inv else 'DISABLED (default)'}",
          file=sys.stderr)
    print(file=sys.stderr)

    if not HAS_FASTMCP:
        import warnings
        warnings.warn(
            "FastMCP not available (pip install 'mcp>=1.0'). "
            "Falling back to minimal stdio MCP server.",
        )
        run_stdio_fallback()
        return

    server = create_fastmcp_server()
    server.run(transport="stdio")


if __name__ == "__main__":
    main()