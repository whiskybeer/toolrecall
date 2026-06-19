"""ToolRecall Client — daemon-first with fallback to direct operations.

Architecture:
  cached_read() → daemon (UDS) → if daemon unavailable → direct SQLite

This module is the primary interface for agents and tools.
Each cached_* function tries the daemon first, then falls back to
local execution when the daemon is not running.
"""

import atexit
import time

from toolrecall.transport import TransportClient, DEFAULT_PATH

# Pre-bind fallback imports from cache/docs modules.
# These are called when the daemon is unavailable.
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
from toolrecall.docs import (
    docs_search as _direct_docs_search,
    docs_get_page as _direct_docs_get_page,
)

# ─── Shared Connection ───────────────────────────────────────

_client: TransportClient = None  # Shared transport connection
_last_check = 0.0

# Pre-bind DEFAULT_PATH as module attribute so _patch_transport in
# tests can overwrite it by setting client.DEFAULT_PATH.
DEFAULT_PATH = _DEFAULT_PATH = DEFAULT_PATH


def _get_client() -> TransportClient:
    """Get or create shared transport connection."""
    global _client, _DEFAULT_PATH
    if _client is None:
        _client = TransportClient(_DEFAULT_PATH)
    return _client


def _check_daemon() -> bool:
    """Quick ping to check if daemon is reachable.

    Uses a dedicated TransportClient to avoid polluting the shared
    connection with health-check messages.
    """
    try:
        tc = TransportClient(_DEFAULT_PATH)
        resp = tc.send({"cmd": "ping"})
        return resp.get("pong", False)
    except Exception:
        return False


# ─── Core API ─────────────────────────────────────────────────


def mcp_call(server: str, tool: str, arguments: dict = None, bypass_cache: bool = False) -> dict:
    """Call a tool on a multiplexed MCP server via the daemon.

    Requires the ToolRecall daemon to be running with MCP Multiplexer enabled.
    Returns the tool call result (cached or fresh).

    Args:
        server: MCP server name (e.g. "github", "time", "fetch")
        tool: Tool name on that server
        arguments: Tool arguments dict
        bypass_cache: If True, skip cache and force a fresh call
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
    """Read file via daemon (UDS) or direct SQLite.

    Daemon-first: sends path to the running daemon over Unix Domain Socket.
    If daemon is unreachable, falls back to direct local SQLite lookup.
    """
    client = _get_client()
    resp = client.send({"cmd": "cached_read", "path": path})
    if "error" not in resp or resp["error"] != "daemon_unavailable":
        return resp  # Success or real error from Daemon
    # Fallback: direct SQLite (no daemon needed)
    return _direct_read(path)


def cached_terminal(command: str, ttl: int = None) -> dict:
    """Run command via daemon or direct SQLite.

    Sends command to daemon for execution caching with optional TTL.
    Falls back to local execution with same TTL logic when daemon is down.
    """
    client = _get_client()
    payload = {"cmd": "cached_terminal", "command": command}
    if ttl is not None:
        payload["ttl"] = ttl
    resp = client.send(payload)
    if "error" not in resp or resp["error"] != "daemon_unavailable":
        return resp
    return _direct_terminal(command, ttl=ttl)


def cached_skill(name: str) -> dict:
    """View skill via daemon or direct SQLite.

    Loads skill content through the daemon's file cache.
    Falls back to direct SQLite lookup when daemon is unavailable.
    """
    client = _get_client()
    resp = client.send({"cmd": "cached_skill", "name": name})
    if "error" not in resp or resp["error"] != "daemon_unavailable":
        return resp
    return _direct_skill(name)


def cached_write(path: str, content: str) -> dict:
    """Write file via daemon or direct — skips write if content matches disk.

    Uses daemon's diff-check to skip writes when file content is unchanged.
    Fallback to direct write when daemon is unavailable.
    """
    client = _get_client()
    resp = client.send({"cmd": "cached_write", "path": path, "content": content})
    if "error" not in resp or resp["error"] != "daemon_unavailable":
        return resp
    return _direct_write(path, content)


def cached_patch(path: str, old_string: str, new_string: str) -> dict:
    """Apply patch via daemon or direct — skips if already applied.

    Uses daemon's diff engine for idempotent patching.
    Falls back to direct patching when daemon is unavailable.
    """
    client = _get_client()
    resp = client.send({"cmd": "cached_patch", "path": path,
                         "old_string": old_string, "new_string": new_string})
    if "error" not in resp or resp["error"] != "daemon_unavailable":
        return resp
    return _direct_patch(path, old_string, new_string)


def docs_search(query: str, source: str = None) -> str:
    """Search knowledge base via daemon or direct SQLite.

    Daemon-first search over indexed docs.
    Falls back to direct SQLite when daemon is not running.
    """
    client = _get_client()
    payload = {"cmd": "docs_search", "query": query}
    if source:
        payload["source"] = source
    resp = client.send(payload)
    if "error" not in resp or resp["error"] != "daemon_unavailable":
        return resp.get("result", str(resp))
    return _direct_docs_search(query, source=source)


def docs_get_page(source: str, path: str) -> str:
    """Get indexed page via daemon or direct SQLite.

    Retrieves a specific page from the daemon's docs index.
    Falls back to direct SQLite when daemon is unavailable.
    """
    client = _get_client()
    resp = client.send({"cmd": "docs_get_page", "source": source, "path": path})
    if "error" not in resp or resp["error"] != "daemon_unavailable":
        return resp.get("result", str(resp))
    return _direct_docs_get_page(source, path)


def cache_status() -> str:
    """Get cache stats via daemon or direct SQLite.

    Daemon-first: returns structured cache stats from the running daemon.
    Falls back to building a local status report when daemon is unavailable.
    """
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
    """Invalidate cache via daemon or direct SQLite.

    Daemon-first: sends invalidation command to the running daemon.
    Falls back to direct SQLite invalidation when daemon is unavailable.
    """
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
    """Override the default transport path (UDS file or tcp://host:port).

    Updates the transport module's DEFAULT_PATH and forces a reconnect
    on the next cached_* call.
    """
    global _client, _DEFAULT_PATH, DEFAULT_PATH
    import toolrecall.transport as _tp
    _tp.DEFAULT_PATH = path
    # Also update the local reference imported at top of client.py
    _reimport_default_path()
    _client = None  # Force reconnect


def _reimport_default_path():
    """Re-read DEFAULT_PATH from transport module into this module's namespace.

    Used by set_socket_path() to ensure _get_client() uses the updated path.
    """
    import toolrecall.transport
    global _DEFAULT_PATH
    _DEFAULT_PATH = toolrecall.transport.DEFAULT_PATH
    # Also update the module-level attribute so _patch_transport in tests
    # can see it as client.DEFAULT_PATH
    DEFAULT_PATH = _DEFAULT_PATH


# Direct fallbacks are imported at the top of this module from cache.py and docs.py.
# These aliases (e.g. _direct_read, _direct_stats) are called when the daemon is
# unreachable. No inline imports needed.


# ─── Auto-Cleanup ──────────────────────────────────


def _cleanup():
    """Close shared transport connection on exit."""
    global _client
    if _client is not None:
        try:
            _client.close()
        except Exception:
            pass
        _client = None


atexit.register(_cleanup)
