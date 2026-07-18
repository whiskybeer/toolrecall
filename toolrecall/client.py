"""ToolRecall Client — daemon-first with fallback to direct operations.

Architecture:
  cached_read() → daemon (UDS) → if daemon unavailable → direct SQLite

This module is the primary interface for agents and tools.
Each cached_* function tries the daemon first, then falls back to
local execution when the daemon is not running.
"""

import atexit

from toolrecall.transport import TransportClient, DEFAULT_PATH

# Lazy fallback imports — imported only when daemon is unavailable.
# This avoids opening a second SQLite connection that conflicts with
# the running daemon's database lock.
_direct_cache = None
_direct_docs = None


def _get_direct_cache():
    """Lazy import toolrecall.cache for daemon-fallback operations."""
    global _direct_cache
    if _direct_cache is None:
        from toolrecall import cache as _direct_cache
    return _direct_cache


def _get_direct_docs():
    """Lazy import toolrecall.docs for daemon-fallback operations."""
    global _direct_docs
    if _direct_docs is None:
        from toolrecall import docs as _direct_docs
    return _direct_docs

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
    Fallback respects allowed_paths by loading the config and checking
    against the same SecurityGate rules the daemon would apply.
    """
    client = _get_client()
    resp = client.send({"cmd": "cached_read", "path": path, "source": "agent_tool"})
    if "error" not in resp or resp["error"] != "daemon_unavailable":
        return resp  # Success or real error from Daemon
    # Fallback: check allowed_paths ourselves, then read directly
    from toolrecall.config import load_config
    cfg = load_config()
    allowed_paths = cfg.mcp_allowed_paths
    if allowed_paths:
        import os as _os
        abs_path = _os.path.realpath(_os.path.expanduser(path))
        for allowed in allowed_paths:
            allowed_abs = _os.path.realpath(_os.path.expanduser(allowed))
            if abs_path == allowed_abs or abs_path.startswith(allowed_abs + _os.sep):
                break
        else:
            return {"error": "Path not allowed: access denied (daemon unavailable)"}
    return _get_direct_cache().cached_read(path, source="agent_tool")


def cached_shell_exec(command: str) -> dict:
    """Execute a wrapped shell command, stripping infrastructure, caching the inner command.

    Agent-agnostic: detects common shell wrappers (source, cd, eval, printf markers, exit)
    from any agent (Hermes, Codex, Claude Code, etc.), strips them, and routes the real
    command through cached_terminal with allowlist security.

    Falls back to direct cached_terminal when daemon is unavailable.
    """
    client = _get_client()
    payload = {"cmd": "cached_shell_exec", "command": command}
    resp = client.send(payload)
    if "error" not in resp or resp["error"] != "daemon_unavailable":
        return resp
    # Fall back to direct wrapper stripping + cached_terminal
    from toolrecall.cache import cached_shell_exec as _direct_shell_exec
    return _direct_shell_exec(command)


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
    # SECURITY: fail-closed on daemon unavailable — terminal is a gated
    # operation that requires the daemon's SecurityGate. Without the
    # daemon, we cannot enforce allow_terminal or allowed_terminal_commands.
    # The user must restart the daemon to use this feature.
    return {"error": "daemon_unavailable: cached_terminal requires the daemon. Restart with 'toolrecall daemon'."}


def cached_skill(name: str) -> dict:
    """View skill via daemon or direct SQLite.

    Loads skill content through the daemon's file cache.
    Falls back to direct SQLite lookup when daemon is unavailable.
    """
    client = _get_client()
    resp = client.send({"cmd": "cached_skill", "name": name})
    if "error" not in resp or resp["error"] != "daemon_unavailable":
        return resp
    return _get_direct_cache().cached_skill(name)


def cached_write(path: str, content: str) -> dict:
    """Write file via daemon or fail closed.

    Uses daemon's diff-check to skip writes when file content is unchanged.
    When daemon is unavailable, returns an error (fail-closed) — the daemon
    is the single source of truth for the path allowlist.
    """
    client = _get_client()
    resp = client.send({"cmd": "cached_write", "path": path, "content": content})
    if "error" not in resp or resp["error"] != "daemon_unavailable":
        return resp
    # SECURITY: fail-closed on daemon unavailable — write is a gated
    # operation that requires the daemon's SecurityGate. Without the
    # daemon, we cannot enforce the path allowlist.
    # The user must restart the daemon to use this feature.
    return {"error": "daemon_unavailable: cached_write requires the daemon. Restart with 'toolrecall daemon'."}


def cached_patch(path: str, old_string: str, new_string: str) -> dict:
    """Apply patch via daemon or fail closed.

    Uses daemon's diff engine for idempotent patching.
    When daemon is unavailable, returns an error (fail-closed) — the daemon
    is the single source of truth for the path allowlist.
    """
    client = _get_client()
    resp = client.send({"cmd": "cached_patch", "path": path,
                         "old_string": old_string, "new_string": new_string})
    if "error" not in resp or resp["error"] != "daemon_unavailable":
        return resp
    # SECURITY: fail-closed on daemon unavailable — patch is a gated
    # operation that requires the daemon's SecurityGate. Without the
    # daemon, we cannot enforce the path allowlist.
    # The user must restart the daemon to use this feature.
    return {"error": "daemon_unavailable: cached_patch requires the daemon. Restart with 'toolrecall daemon'."}


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
    return _get_direct_docs().docs_search(query, source=source)


def docs_get_page(path: str, source: str = "") -> str:
    """Get indexed page via daemon or direct SQLite.

    Retrieves a specific page from the daemon's docs index.
    Falls back to direct SQLite when daemon is unavailable.
    """
    client = _get_client()
    resp = client.send({"cmd": "docs_get_page", "source": source, "path": path})
    if "error" not in resp or resp["error"] != "daemon_unavailable":
        return resp.get("result", str(resp))
    return _get_direct_docs().docs_get_page(path, source)


def cache_status() -> str:
    """Get cache stats via daemon or direct SQLite.

    Daemon-first: returns structured cache stats from the running daemon.
    Falls back to building a local status report when daemon is unavailable.
    """
    client = _get_client()
    resp = client.send({"cmd": "cache_status"})
    if "error" not in resp or resp["error"] != "daemon_unavailable":
        return resp.get("result", str(resp))
    stats = _get_direct_cache().get_stats()
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
    _get_direct_cache().invalidate_all()
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
    return _get_direct_cache().refresh_file(path)


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


# ─── Context Tracker API ────────────────────────────────


def context_set_checkpoint(name: str = "") -> dict:
    """Set a checkpoint — mark current state as clean.

    The agent calls this after reading all necessary files for a task phase.
    Everything read before this point is "clean" and safe to drop.
    Files written/patched after this point will be tracked as "dirty".

    Args:
        name: Optional human-readable label for the checkpoint.

    Returns:
        {"checkpoint": int, "name": str, "dirty_before": int}
    """
    client = _get_client()
    payload = {"cmd": "context_set_checkpoint", "name": name}
    resp = client.send(payload)
    return resp


def context_get_dirty(checkpoint: int = None) -> dict:
    """Get dirty and clean files since a checkpoint.

    Dirty = files that were written/patched since the checkpoint.
    Clean = files that were read but NOT written since the checkpoint.
    The agent should KEEP dirty files and may DROP clean ones.

    Args:
        checkpoint: Checkpoint ID to diff against. None = use current.

    Returns:
        {
            "dirty": ["/abs/path", ...],
            "clean": ["/abs/path", ...],
            "checkpoint": int,
            "total_dirty": int,
            "total_clean": int,
        }
    """
    client = _get_client()
    payload = {"cmd": "context_get_dirty"}
    if checkpoint is not None:
        payload["checkpoint"] = checkpoint
    resp = client.send(payload)
    return resp


def context_get_stats() -> dict:
    """Full status of the context tracker.

    Returns:
        {
            "dirty": [...],
            "clean": [...],
            "checkpoint": int,
            "total_dirty": int,
            "total_clean": int,
            "total_read": int,
        }
    """
    client = _get_client()
    payload = {"cmd": "context_get_stats"}
    resp = client.send(payload)
    return resp


def context_reset() -> dict:
    """Clear all checkpoints and dirty state.

    After reset, the tracker behaves as if freshly initialized.
    Call context_set_checkpoint() again before starting work.

    Returns:
        {"reset": True, "checkpoint": 0}
    """
    client = _get_client()
    payload = {"cmd": "context_reset"}
    resp = client.send(payload)
    return resp


# Direct fallbacks are lazy-imported via _get_direct_cache() / _get_direct_docs().
# This avoids opening a second SQLite connection when the daemon is already running.
# The cache module (and its DB connection) is only loaded when the daemon is
# unreachable and we need to operate directly on the database.


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
