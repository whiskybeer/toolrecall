"""ToolRecall Cache FS MCP Server — cached read/write via ToolRecall SQLite.

Provides MCP tools that cache file reads and terminal commands through
ToolRecall's daemon. This is agent-agnostic — any MCP-compatible agent
(opencode, Claude Code, Cursor, etc.) gets transparent caching.

The tools use native names (read_file, write_file, patch, terminal) so
agents pick them naturally — no cached_ prefix needed.

Architecture:
  ┌─────────────┐     MCP stdio      ┌──────────────────────┐
  │   Agent     │ ◄────────────────► │ ToolRecall Cache FS  │
  │ (opencode,  │                    │   MCP Server         │
  │  Claude,    │     tools:         │                      │
  │  Cursor)    │     read_file      │  ┌────────────────┐  │
  │             │     write_file     │  │ ToolRecall     │  │
  │             │     patch          │  │ Daemon (UDS)   │  │
  │             │     terminal       │  └────────────────┘  │
  └─────────────┘                    └──────────────────────┘

The MCP server connects to the running ToolRecall daemon via UDS.
If the daemon isn't running, it starts it automatically (_ensure_daemon).

Note: This server is most useful as a standalone MCP tool set for agents
that don't use the ToolRecall multiplexer. If you use the multiplexer
(`toolrecall mcp`), the bridge already provides these tools natively
along with cache status, docs search, and MCP server management.
"""

import json
import os
import sys
import logging

_LOG: logging.Logger | None = None


# ─── ToolRecall Daemon Client ─────────────────────────────────────────

def _ensure_daemon() -> bool:
    """Ensure the ToolRecall daemon is running. Returns True if ready."""
    from toolrecall.transport import TransportClient, DEFAULT_PATH
    import time

    try:
        tc = TransportClient(DEFAULT_PATH)
        resp = tc.send({"cmd": "ping"})
        if resp.get("pong"):
            return True
    except Exception:
        pass

    # Remove stale socket before starting daemon
    if os.path.exists(DEFAULT_PATH):
        try:
            os.unlink(DEFAULT_PATH)
        except OSError:
            pass

    # Auto-start via subprocess
    import subprocess
    import shutil
    try:
        toolrecall_bin = shutil.which("toolrecall")
        if not toolrecall_bin:
            return False
        subprocess.Popen(
            [toolrecall_bin, "daemon", "--foreground"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        for _ in range(10):
            time.sleep(0.5)
            try:
                tc = TransportClient(DEFAULT_PATH)
                resp = tc.send({"cmd": "ping"})
                if resp.get("pong"):
                    return True
            except Exception:
                continue
    except Exception:
        pass
    return False


def _daemon_send(cmd: dict) -> dict:
    """Send a command to the ToolRecall daemon and return the response."""
    from toolrecall.transport import TransportClient, DEFAULT_PATH
    tc = TransportClient(DEFAULT_PATH)
    return tc.send(cmd)


# ─── MCP Tool Implementations ─────────────────────────────────────────

def _cached_read(path: str, max_tokens: int = 0) -> str:
    """Read a file through ToolRecall's cache.

    Args:
        path: Absolute path to the file to read
        max_tokens: Max tokens to return (0 = unlimited, entire file)

    Returns:
        File content (cached if available)
    """
    # Expand ~
    path = os.path.expanduser(path)

    # Check if file exists
    if not os.path.isfile(path):
        return f"Error: file not found: {path}"

    # Read via daemon for caching
    resp = _daemon_send({
        "cmd": "cached_read",
        "path": path,
        "max_tokens": max_tokens,
    })

    if resp.get("error"):
        return f"Error: {resp['error']}"

    content = resp.get("content", "")

    return content


def _cached_terminal(command: str, timeout: int = 30) -> str:
    """Run a terminal command through ToolRecall's cache.

    Args:
        command: Shell command to run (e.g., 'ls -la', 'uname -a')
        timeout: Max seconds to wait (default: 30, max: 300)

    Returns:
        Command output (cached if available)
    """
    resp = _daemon_send({
        "cmd": "cached_terminal",
        "command": command,
        "timeout": timeout,
    })

    if resp.get("error"):
        return f"Error: {resp['error']}"

    output = resp.get("output", "")

    return output


def _cached_write(path: str, content: str) -> str:
    """Write content to a file and invalidate the cache entry.

    This does NOT cache the write — it invalidates any cached read
    for the same path so the next read is fresh. Returns the write result.

    SECURITY: Routes the write through the daemon (which enforces the
    path allowlist and sensitive-file blocklist) instead of writing
    directly. Previously wrote to any path with no security checks,
    allowing writes to ~/.ssh/authorized_keys etc.

    Args:
        path: Absolute path to write to
        content: Content to write

    Returns:
        Success/error message
    """
    # SECURITY: Route through daemon — it enforces the path allowlist
    # and sensitive-file blocklist. We must NOT write directly here.
    resp = _daemon_send({
        "cmd": "cached_write",
        "path": path,
        "content": content,
    })

    if resp.get("error"):
        return f"Error: {resp['error']}"

    result = resp.get("result", resp)
    if isinstance(result, dict) and result.get("cached") is False:
        return f"Written {len(content)} bytes to {path}"
    elif isinstance(result, dict) and result.get("unchanged"):
        return f"File unchanged (content already matches): {path}"
    elif isinstance(result, dict) and result.get("error"):
        return f"Error: {result['error']}"

    return f"Written {len(content)} bytes to {path}"


def _cached_patch(path: str, old_string: str, new_string: str) -> str:
    """Apply a find-and-replace patch to a file, invalidating the cache.

    SECURITY: Routes through the daemon (which enforces the path allowlist
    and sensitive-file blocklist) instead of writing directly.

    Args:
        path: Absolute path to the file to patch
        old_string: The exact text to find (must be unique)
        new_string: The replacement text

    Returns:
        Result message with diff info
    """
    # SECURITY: Route through daemon — it enforces the path allowlist
    # and sensitive-file blocklist.
    resp = _daemon_send({
        "cmd": "cached_patch",
        "path": path,
        "old_string": old_string,
        "new_string": new_string,
    })

    if resp.get("error"):
        return f"Error: {resp['error']}"

    result = resp.get("result", resp)
    if isinstance(result, dict):
        if result.get("error"):
            return f"Error: {result['error']}"
        if result.get("unchanged"):
            reason = result.get("reason", "unknown")
            return f"File unchanged ({reason}): {path}"
        if result.get("cached") is False:
            changes = result.get("changes", 1)
            return f"Patched {path}: replaced {len(old_string)} chars with {len(new_string)} chars ({changes} match)"

    return f"Patched {path}: replaced {len(old_string)} chars with {len(new_string)} chars"


# ─── MCP Protocol Handlers ────────────────────────────────────────────

TOOLS = [
    {
        "name": "read_file",
        "description": "Read a file through ToolRecall's cache. "
                       "On first read, content is fetched from disk and cached. "
                       "Subsequent reads return cached content instantly. "
                       "Supports all paths in allowed_paths (~, /etc, /dev). "
                       "Sensitive files (.env, .ssh/, .pem) are still blocked.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Absolute path to the file to read (e.g., /home/user/file.txt, /etc/os-release)",
                },
                "max_tokens": {
                    "type": "integer",
                    "description": "Max tokens to return (0 = entire file)",
                    "default": 0,
                },
            },
            "required": ["path"],
        },
    },
    {
        "name": "terminal",
        "description": "Run a terminal command through ToolRecall's cache. "
                       "On first run, output is fetched and cached. "
                       "Subsequent identical commands return cached output. "
                       "TTL: 5 min for unknown commands, 1h for hostname/whoami/pwd/uname.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "Shell command to run (e.g., 'ls -la /tmp', 'uname -a', 'cat /etc/os-release')",
                },
                "timeout": {
                    "type": "integer",
                    "description": "Max seconds to wait (default: 30)",
                    "default": 30,
                },
            },
            "required": ["command"],
        },
    },
    {
        "name": "write_file",
        "description": "Write content to a file. Invalidates the cache entry "
                       "so the next read_file is fresh. Does NOT cache the write itself.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Absolute path to write to",
                },
                "content": {
                    "type": "string",
                    "description": "Content to write to the file",
                },
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "patch",
        "description": "Apply a find-and-replace patch to a file. "
                       "Invalidates the cache entry so the next read_file is fresh. "
                       "The old_string must be unique in the file.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Absolute path to the file to patch",
                },
                "old_string": {
                    "type": "string",
                    "description": "The exact text to find and replace (must be unique)",
                },
                "new_string": {
                    "type": "string",
                    "description": "The replacement text",
                },
            },
            "required": ["path", "old_string", "new_string"],
        },
    },
]


def _handle_tool_call(name: str, arguments: dict) -> dict:
    """Route a tool call to the right implementation."""
    try:
        if name == "read_file":
            content = _cached_read(
                path=arguments["path"],
                max_tokens=arguments.get("max_tokens", 0),
            )
            return {"content": [{"type": "text", "text": content}]}

        elif name == "terminal":
            output = _cached_terminal(
                command=arguments["command"],
                timeout=arguments.get("timeout", 30),
            )
            return {"content": [{"type": "text", "text": output}]}

        elif name == "write_file":
            result = _cached_write(
                path=arguments["path"],
                content=arguments["content"],
            )
            return {"content": [{"type": "text", "text": result}]}

        elif name == "patch":
            result = _cached_patch(
                path=arguments["path"],
                old_string=arguments["old_string"],
                new_string=arguments["new_string"],
            )
            return {"content": [{"type": "text", "text": result}]}

        else:
            return {"content": [{"type": "text", "text": f"Unknown tool: {name}"}],
                    "isError": True}

    except Exception as e:
        return {"content": [{"type": "text", "text": f"Error: {e}"}],
                "isError": True}


# ─── MCP Server Loop ──────────────────────────────────────────────────

def main():
    """Run the MCP Cache FS server over stdin/stdout (stdio MCP protocol)."""
    # Ensure daemon is running
    if not _ensure_daemon():
        # Write only to stderr — stdout is reserved for MCP protocol
        print("Warning: ToolRecall daemon not available. Running uncached.", file=sys.stderr)

    # Send server info (MCP initialization)
    server_info = {
        "protocolVersion": "2025-03-26",
        "capabilities": {
            "tools": {
                "listChanged": False,
            },
        },
        "serverInfo": {
            "name": "toolrecall-cache-fs",
            "version": "0.8.6",
        },
    }

    # Main MCP loop — read JSON-RPC from stdin, write responses to stdout
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue

        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue

        msg_id = msg.get("id")
        method = msg.get("method")

        response = {"jsonrpc": "2.0", "id": msg_id}

        if method == "initialize":
            response["result"] = server_info

        elif method == "notifications/initialized":
            # No response needed for initialized notification
            continue

        elif method == "tools/list":
            response["result"] = {"tools": TOOLS}

        elif method == "tools/call":
            params = msg.get("params", {})
            name = params.get("name", "")
            arguments = params.get("arguments", {})
            response["result"] = _handle_tool_call(name, arguments)

        elif method == "ping":
            response["result"] = {}

        else:
            response["result"] = {}
            response["error"] = {"code": -32601, "message": f"Method not found: {method}"}

        # Write response to stdout — must flush for MCP stdio protocol
        sys.stdout.write(json.dumps(response) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()