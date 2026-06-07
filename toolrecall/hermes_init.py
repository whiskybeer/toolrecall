# ToolRecall Hermes Init — registers cached_* tools via Daemon Client.
#
# This init script registers cached_read and cached_terminal as
# explicit, separate tools in the Hermes Tool Registry. The tools forward
# requests to the ToolRecall Daemon (UDS) — or use direct SQLite as a fallback.
#
# Installation:
#   bash <(curl -s https://raw.githubusercontent.com/whiskybeer/toolrecall/main/setup.sh)
#
# Or manually:
#   pip install toolrecall
#   hermes config set agent.init_scripts '["~/.toolrecall/hermes_init.py"]'
#
# Then restart Hermes or /reset.

import os
import sys

# ─── 1. Import ToolRecall Client ─────────────────────────────
try:
    from toolrecall.client import (
        cached_read, cached_terminal, cached_skill,
        docs_search, cache_status, daemon_running,
    )
    TOOLRECALL_AVAILABLE = True
except ImportError:
    TOOLRECALL_AVAILABLE = False
    cached_read = None
    cached_terminal = None
    cached_skill = None
    docs_search = None
    cache_status = None
    daemon_running = None


# ─── 2. Register separate, explicit cached tools ─────────────

def _register_tools():
    """Register cached_read + cached_terminal as Hermes tools."""
    if not TOOLRECALL_AVAILABLE or cached_read is None or cached_terminal is None:
        return

    try:
        from tools.registry import registry

        registry.register(
            name="cached_read",
            toolset="file",
            schema={
                "name": "cached_read",
                "description": "Read a text file with caching (mtime-based). "
                               "Use via Daemon (UDS) or direct SQLite fallback.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Path to the file"}
                    },
                    "required": ["path"]
                }
            },
            handler=lambda args, **kw: cached_read(args.get("path", "")),
            emoji="⚡"
        )

        registry.register(
            name="cached_terminal",
            toolset="terminal",
            schema={
                "name": "cached_terminal",
                "description": "Run a terminal command with TTL caching. "
                               "Use via Daemon (UDS) or direct SQLite fallback.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "command": {"type": "string", "description": "Command"},
                        "ttl": {"type": "integer", "description": "TTL in seconds"}
                    },
                    "required": ["command"]
                }
            },
            handler=lambda args, **kw: cached_terminal(args.get("command", ""), ttl=args.get("ttl")),
            emoji="⚡"
        )
    except Exception:
        pass


# ─── 3. Run on load ──────────────────────────────────────────

if TOOLRECALL_AVAILABLE:
    _register_tools()

    daemon_active = daemon_running() if daemon_running else False

    print(f"  {'='*44}")
    print(f"  ToolRecall Caching Registered")
    print(f"  Tools: cached_read, cached_terminal")
    if daemon_active:
        print(f"  Mode:  Daemon (UDS) — shared cache")
    else:
        print(f"  Mode:  Direct SQLite — no daemon")
    print(f"  {'='*44}")
    print()
else:
    print("  ToolRecall not installed. Run: pip install toolrecall")
    print()