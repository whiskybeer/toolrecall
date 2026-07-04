"""ToolRecall MCP Server Registry — auto-resolve server names to commands.

Built-in servers ship with ToolRecall (zero dependencies, stdlib only).
External servers are resolved via uvx by default; users can override
via [mcp_multiplex.servers_config] in config.toml.

Usage:
    from toolrecall.mcp_registry import resolve_server
    cmd, args = resolve_server("fetch")       # -> ("uvx", ["mcp-server-fetch"])
    cmd, args = resolve_server("time")         # -> ("python3", ["-m", "toolrecall.mcp_time"])
    cmd, args = resolve_server("custom-thing") # -> None (not found)
"""

import os
import sys
from pathlib import Path

# ─── Built-in servers: ship with ToolRecall, no external dependencies ───
# Key = server name (lowercase), value = (command, [args...])

BUILTIN_SERVERS: dict[str, tuple[str, list[str]]] = {
    "time": (sys.executable, ["-m", "toolrecall.mcp_time"]),
    "sequential-thinking": (sys.executable, ["-m", "toolrecall.mcp_seqthink"]),
    "github": (sys.executable, ["-m", "toolrecall.mcp_github"]),
    "fetch": (sys.executable, ["-m", "toolrecall.mcp_fetch"]),
    "cache-fs": (sys.executable, ["-m", "toolrecall.mcp_cache_fs"]),
}

# ─── External server registry: well-known MCP servers via uvx ───
# Users can override any of these via [mcp_multiplex.servers_config].
# uvx is NOT a dependency of ToolRecall — users install it themselves.
# If uvx is not found, a helpful error is returned.

EXTERNAL_REGISTRY: dict[str, tuple[str, list[str]]] = {
    "filesystem": ("uvx", ["mcp-server-filesystem"]),
    "git": ("uvx", ["mcp-server-git"]),
    "memory": ("uvx", ["mcp-server-memory"]),
    "everything": ("uvx", ["mcp-server-everything"]),
    "brave-search": ("uvx", ["@anthropic/mcp-server-brave-search"]),
    "playwright": ("uvx", ["@playwright/mcp"]),
    "slack": ("uvx", ["mcp-server-slack"]),
}

# ─── Combined lookup ───

_ALL: dict[str, tuple[str, list[str], str]] = {}  # name -> (cmd, args, source)

for name, spec in BUILTIN_SERVERS.items():
    _ALL[name] = (*spec, "builtin")
for name, spec in EXTERNAL_REGISTRY.items():
    _ALL[name] = (*spec, "external")


def resolve_server(name: str) -> tuple[str, list[str], str] | None:
    """Resolve a server name to (command, args, source).

    Args:
        name: Server name (case-insensitive, e.g. "fetch", "Time", "SEQUENTIAL-THINKING")

    Returns:
        (command, args, source) where source is "builtin", "external", or "config".
        Returns None if the server is not found in any registry.

    Example:
        >>> resolve_server("time")
        ("/usr/bin/python3", ["-m", "toolrecall.mcp_time"], "builtin")
        >>> resolve_server("fetch")
        ("uvx", ["mcp-server-fetch"], "external")
        >>> resolve_server("nope")
        None
    """
    result = _ALL.get(name.lower())
    if result is not None:
        # Return (cmd, args, source) — caller checks source if needed
        return (result[0], list(result[1]), result[2])
    return None


def is_builtin(name: str) -> bool:
    """Check if a server name is a built-in (no external deps)."""
    return name.lower() in BUILTIN_SERVERS


def is_known(name: str) -> bool:
    """Check if a server name is in any registry (built-in or external)."""
    return name.lower() in _ALL


def list_registered_servers() -> list[dict]:
    """Return all known servers with metadata.

    Returns list of dicts: {name, source, command, args}
    """
    result = []
    for name in sorted(_ALL.keys()):
        cmd, args, source = _ALL[name]
        result.append({
            "name": name,
            "source": source,
            "command": cmd,
            "args": list(args),
        })
    return result


def has_uvx() -> bool:
    """Check if uvx is available on PATH."""
    for path_dir in os.environ.get("PATH", "").split(os.pathsep):
        if not path_dir:
            continue
        uvx_path = Path(path_dir) / "uvx"
        if uvx_path.exists():
            return True
        # Windows
        uvx_exe = Path(path_dir) / "uvx.exe"
        if uvx_exe.exists():
            return True
    return False
