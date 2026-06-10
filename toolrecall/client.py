"""ToolRecall Client — Platform-agnostic IPC client.

Connects via TransportClient (UDS on POSIX, TCP on Windows).
Falls back to direct SQLite when the daemon is unavailable.

Usage:
    from toolrecall.client import (
        cached_read, cached_terminal, cached_skill,
        docs_search, docs_get_page, cache_status, cache_invalidate,
    )
"""

import json
import os
import sys
from pathlib import Path

from toolrecall.transport import TransportClient, DEFAULT_PATH

from toolrecall.cache import (
    cached_read as _direct_read,
    cached_terminal as _direct_terminal,
    cached_skill as _direct_skill,
    cached_write as _direct_write,
    cached_patch as _direct_patch,
    invalidate_all as _direct_invalidate,
    refresh_file as _direct_refresh,
    get_stats as _direct_stats,
)
from toolrecall.docs import docs_search as _direct_docs_search, docs_get_page as _direct_docs_get_page
from toolrecall.config import load_config

# ─── Shared Client Instance ───────────────────────────────

_client = None


def _get_client() -> TransportClient:
    """Get or create the shared TransportClient."""
    global _client
    if _client is None:
        _client = TransportClient(DEFAULT_PATH)
    return _client


def _check_daemon() -> bool:
    """Check if Daemon is running (fast ping)."""
    return TransportClient(DEFAULT_PATH).ping()


# ─── Public API (Daemon first, fallback to direct SQLite) ──


def mcp_call(server: str, tool: str, arguments: dict = None, bypass_cache: bool = False) -> dict:
    """Call a tool on a multiplexed MCP server via the daemon.

    Requires the ToolRecall daemon to be running with MCP Multiplexer enabled.
    Returns the tool call result (cached or fresh).

    Args:
        server: MCP server name (e.g. "github", "time", "fetch")
        tool: Tool name on that server
        arguments: Tool arguments dict
        bypass_cache: If True, skip cache and force a fresh call

    Usage:
        from toolrecall.client import mcp_call
        result = mcp_call("github", "list_issues",
                          {"owner": "whiskybeer", "repo": "toolrecall"})
    """
    client = _get_client()
    payload = {
        "cmd": "mcp_call",
        "server": server,
        "tool": tool,
        "arguments": arguments or {},
    }
    if bypass_cache:
        payload["ttl"] = 0

    resp = client.send(payload)
    if "error" in resp and resp["error"] == "MCP multiplexer is disabled.":
        return {"error": "MCP multiplexer is not enabled in ToolRecall config."}
    return resp


def mcp_list_servers() -> dict:
    """List available multiplexed MCP servers and their tools.

    Returns dict with "result" containing a list of server info dicts.
    Each server info includes name, running status, and tool names.
    """
    client = _get_client()
    payload = {"cmd": "mcp_list_servers"}
    resp = client.send(payload)
    return resp


def cached_read(path: str) -> dict:
    """Read file via Daemon (UDS) or direktes SQLite."""
    client = _get_client()
    resp = client.send({"cmd": "cached_read", "path": path})
    if "error" not in resp or resp["error"] != "daemon_unavailable":
        return resp  # Success or real error from Daemon

    # Fallback: direktes SQLite
    return _direct_read(path)


def cached_terminal(command: str, ttl: int = None) -> dict:
    """Run command via Daemon or direktem SQLite."""
    client = _get_client()
    payload = {"cmd": "cached_terminal", "command": command}
    if ttl is not None:
        payload["ttl"] = ttl
    resp = client.send(payload)
    if "error" not in resp or resp["error"] != "daemon_unavailable":
        return resp
    return _direct_terminal(command, ttl=ttl)


def cached_skill(name: str) -> dict:
    """View skill via Daemon or direktem SQLite."""
    client = _get_client()
    resp = client.send({"cmd": "cached_skill", "name": name})
    if "error" not in resp or resp["error"] != "daemon_unavailable":
        return resp
    return _direct_skill(name)


def cached_write(path: str, content: str) -> dict:
    """Write file via Daemon or direct — skips write if content matches disk."""
    client = _get_client()
    resp = client.send({"cmd": "cached_write", "path": path, "content": content})
    if "error" not in resp or resp["error"] != "daemon_unavailable":
        return resp
    return _direct_write(path, content)


def cached_patch(path: str, old_string: str, new_string: str) -> dict:
    """Apply patch via Daemon or direct — skips if already applied."""
    client = _get_client()
    resp = client._send({"cmd": "cached_patch", "path": path,
                         "old_string": old_string, "new_string": new_string})
    if "error" not in resp or resp["error"] != "daemon_unavailable":
        return resp
    return _direct_patch(path, old_string, new_string)


def docs_search(query: str, source: str = None) -> str:
    """Search knowledge base via Daemon or direktem SQLite."""
    client = _get_client()
    payload = {"cmd": "docs_search", "query": query}
    if source:
        payload["source"] = source
    resp = client.send(payload)
    if "error" not in resp or resp["error"] != "daemon_unavailable":
        return resp.get("result", str(resp))
    return _direct_docs_search(query, source=source)


def docs_get_page(source: str, path: str) -> str:
    """Get indexed page via Daemon or direktem SQLite."""
    client = _get_client()
    resp = client._send({"cmd": "docs_get_page", "source": source, "path": path})
    if "error" not in resp or resp["error"] != "daemon_unavailable":
        return resp.get("result", str(resp))
    return _direct_docs_get_page(source, path)


def cache_status() -> str:
    """Get cache stats via Daemon or direktem SQLite."""
    client = _get_client()
    resp = client.send({"cmd": "cache_status"})
    if "error" not in resp or resp["error"] != "daemon_unavailable":
        return resp.get("result", str(resp))
    stats = _direct_stats()
    lines = ["ToolRecall Cache Status", "=" * 40]
    for k, v in stats.items():
        if isinstance(v, dict):
            lines.append(f"\n{k}:")
            for sk, sv in v.items():
                lines.append(f"  {sk}: {sv}")
        else:
            lines.append(f"{k}: {v}")
    return "\n".join(lines)


def cache_invalidate() -> str:
    """Invalidate cache via Daemon or direktem SQLite."""
    client = _get_client()
    resp = client.send({"cmd": "cache_invalidate"})
    if "error" not in resp or resp["error"] != "daemon_unavailable":
        return resp.get("result", "Cache invalidated via daemon")
    _direct_invalidate()
    return "Cache invalidated (direct)"


def refresh_file(path: str) -> dict:
    """Invalidate and re-read a single file via Daemon or direct SQLite.

    Always returns a fresh result (cached: False).
    Respects the path allowlist when going through the daemon.
    """
    client = _get_client()
    resp = client.send({"cmd": "cache_refresh_file", "path": path})
    if "error" not in resp or resp["error"] != "daemon_unavailable":
        return resp
    return _direct_refresh(path)


# ─── Connection Check ─────────────────────────────────────

def daemon_running() -> bool:
    """Check if the ToolRecall daemon is currently running."""
    return _check_daemon()


# ─── Set transport path (for testing / custom setups) ──────

def set_socket_path(path: str):
    """Override the default transport path (UDS file or tcp://host:port)."""
    from toolrecall.transport import DEFAULT_PATH as _DP
    global _client
    # Swap DEFAULT_PATH
    import toolrecall.transport as _tp
    _tp.DEFAULT_PATH = path
    _client = None  # Force reconnect
