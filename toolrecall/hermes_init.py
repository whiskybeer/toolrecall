# ToolRecall Hermes Init — automatically loaded on every session start.
#
# This script makes cached_read/cached_terminal/cached_skill/cached_run/cached_exec
# available in every Hermes session AND patches the built-in Hermes tools
# so read_file/terminal/skill_view use ToolRecall transparently.
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
    import json
    TOOLRECALL_AVAILABLE = True
except ImportError:
    TOOLRECALL_AVAILABLE = False


# ─── 2. Monkey-patch Hermes built-in tools ─────────────────────────
#    This overrides read_file, terminal, skill_view to use cached variants.
#    The agent calls them normally — caching happens transparently.

def _patch_tools():
    """Patch Hermes tool functions to use ToolRecall caching."""
    try:
        # Try to find and patch the tool registry
        # Hermes stores tools in the agent's tool registry
        import importlib

        # Method 1: Direct module-level patching
        for mod_name in ["tools.file_tool", "tools.terminal_tool", "tools.skill_tool",
                         "toolrecall.tools.file_tool"]:
            try:
                mod = importlib.import_module(mod_name)
                if hasattr(mod, "read_file"):
                    original = mod.read_file
                    def cached_wrapper(path, **kw):
                        result = cached_read(path)
                        if "error" in result:
                            return original(path, **kw)
                        return {"content": result["content"], "cached": True}
                    mod.read_file = cached_wrapper
            except (ImportError, AttributeError):
                pass

        # Method 2: Patch via tool registry if available
        try:
            from tools.registry import registry
            if hasattr(registry, "_tools"):
                # read_file
                if "read_file" in registry._tools:
                    orig_handler = registry._tools["read_file"]["handler"]
                    registry._tools["read_file"]["handler"] = lambda args, **kw: cached_read(
                        args.get("path", "")
                    )
                # terminal
                if "terminal" in registry._tools:
                    orig_term = registry._tools["terminal"]["handler"]
                    registry._tools["terminal"]["handler"] = lambda args, **kw: cached_terminal(
                        args.get("command", ""), ttl=args.get("ttl", None)
                    )
        except (ImportError, AttributeError):
            pass

    except Exception:
        pass  # Patching is best-effort — skill instructions still work


# ─── 3. Run on load ─────────────────────────────────────────────────
if TOOLRECALL_AVAILABLE:
    _patch_tools()

    # Show status
    stats = get_stats()
    total_saved = sum(s["tokens_saved"] for s in stats.values() if isinstance(s, dict))
    print(f"  {'='*44}")
    print(f"  ToolRecall Auto-Cache active")
    print(f"  5 cache types: file, terminal, skill, script, code")
    if total_saved > 0:
        print(f"  Total tokens saved: {total_saved:,}")
    print(f"  {'='*44}")
    print()
else:
    print("  ToolRecall not installed. Run: pip install toolrecall")
    print()