# ToolRecall Hermes Init — automatically loaded on every session start.
#
# This script makes cached_read/cached_terminal/cached_skill/cached_run/cached_exec
# available in every Hermes session AND registers cached_read/cached_terminal
# as explicit, separate tools in the Hermes registry (No Monkey-patching!).
#
# Installation (one-time):
#   bash <(curl -s https://raw.githubusercontent.com/Robin/toolrecall/main/setup.sh)
#
# Or manually:
#   pip install toolrecall
#   hermes config set agent.init_scripts ["~/.toolrecall/hermes_init.py"]
#
# Then restart Hermes or run /reset.

import os
import sys

# ─── 1. Import ToolRecall ──────────────────────────────────────────
try:
    from toolrecall import cached_read, cached_terminal, cached_skill
    from toolrecall import cached_run, cached_exec, docs_search
    from toolrecall.cache import get_stats
    TOOLRECALL_AVAILABLE = True
except ImportError:
    TOOLRECALL_AVAILABLE = False
    cached_read = None
    cached_terminal = None
    cached_skill = None
    cached_run = None
    cached_exec = None
    docs_search = None
    get_stats = None


# ─── 2. Register separate, explicit cached tools ────────────────────

def _register_tools():
    """Register explicit cached tools to the Hermes tool registry."""
    if not TOOLRECALL_AVAILABLE or cached_read is None or cached_terminal is None:
        return
        
    try:
        from tools.registry import registry
        
        CACHED_READ_SCHEMA = {
            "name": "cached_read",
            "description": "Read a text file with caching (mtime-based). Use this when loading large static documents or configurations that do not change during the session to save tokens.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path to the file to read (absolute, relative, or ~/path)"}
                },
                "required": ["path"]
            }
        }
        
        CACHED_TERMINAL_SCHEMA = {
            "name": "cached_terminal",
            "description": "Run a terminal/shell command with caching based on the command and a TTL. Use this for static commands like uname, hostname, or pwd to save tokens and time.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "The command to execute"},
                    "ttl": {"type": "integer", "description": "Optional TTL in seconds. Overrides the default TTL for this command."}
                },
                "required": ["command"]
            }
        }
        
        # Register cached_read as a separate, explicit tool
        registry.register(
            name="cached_read",
            toolset="file",
            schema=CACHED_READ_SCHEMA,
            handler=lambda args, **kw: cached_read(args.get("path", "")),
            emoji="⚡"
        )
        
        # Register cached_terminal as a separate, explicit tool
        registry.register(
            name="cached_terminal",
            toolset="terminal",
            schema=CACHED_TERMINAL_SCHEMA,
            handler=lambda args, **kw: cached_terminal(args.get("command", ""), ttl=args.get("ttl")),
            emoji="⚡"
        )
    except Exception:
        pass


# ─── 3. Run on load ─────────────────────────────────────────────────
if TOOLRECALL_AVAILABLE:
    _register_tools()

    # Show status
    if get_stats is not None:
        stats = get_stats()
        total_saved = sum(s["tokens_saved"] for s in stats.values() if isinstance(s, dict))
    else:
        total_saved = 0
        
    print(f"  {'='*44}")
    print(f"  ToolRecall Caching Registered (Safe-by-Default)")
    print(f"  Separate tools: cached_read, cached_terminal")
    if total_saved > 0:
        print(f"  Total tokens saved: {total_saved:,}")
    print(f"  {'='*44}")
    print()
else:
    print("  ToolRecall not installed. Run: pip install toolrecall")
    print()
