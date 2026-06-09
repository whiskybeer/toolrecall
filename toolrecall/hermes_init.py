# ToolRecall Hermes Init — registers cached_* tools via Daemon Client.
#
# This init script registers cached_read, cached_terminal, cached_write,
# and cached_patch as explicit, separate tools in the Hermes Tool Registry.
# The tools forward requests to the ToolRecall Daemon (UDS) — or use direct
# SQLite as a fallback.
#
# In "write" transparent_cache mode, it also monkey-patches Hermes' built-in
# write_file and patch tools to skip redundant operations automatically.
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
        cached_write, cached_patch,
        docs_search, cache_status, daemon_running,
    )
    TOOLRECALL_AVAILABLE = True
except ImportError:
    TOOLRECALL_AVAILABLE = False
    cached_read = cached_terminal = cached_skill = None
    cached_write = cached_patch = None
    docs_search = cache_status = daemon_running = None


# ─── 2. Detect cache mode from config ────────────────────────

def _get_cache_mode() -> str:
    """Read ToolRecall's transparent_cache mode for Hermes.

    Returns: "separate" (default), "transparent", or "write".
    """
    cfg_path = os.environ.get(
        "TOOLRECALL_CONFIG",
        os.path.expanduser("~/.toolrecall/config.toml")
    )
    if not os.path.exists(cfg_path):
        return "separate"

    try:
        import tomllib  # Python 3.11+
        with open(cfg_path, "rb") as f:
            cfg = tomllib.load(f)
        hermes = cfg.get("hermes", {})
        mode = hermes.get("transparent_cache", "separate")
        return mode
    except Exception:
        return "separate"


# ─── 3. Register separate, explicit cached tools ─────────────

def _register_tools():
    """Register cached_read, cached_terminal, cached_write, cached_patch as Hermes tools."""
    if not TOOLRECALL_AVAILABLE:
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

        registry.register(
            name="cached_write",
            toolset="file",
            schema={
                "name": "cached_write",
                "description": "Write a file, skipping if content is identical to disk. "
                               "Saves tokens and halts write-loop waste.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Path to the file"},
                        "content": {"type": "string", "description": "Content to write"}
                    },
                    "required": ["path", "content"]
                }
            },
            handler=lambda args, **kw: cached_write(
                args.get("path", ""), args.get("content", "")
            ),
            emoji="⚡"
        )

        registry.register(
            name="cached_patch",
            toolset="file",
            schema={
                "name": "cached_patch",
                "description": "Apply a find-and-replace patch, skipping if already applied "
                               "or if the target string is not found.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "File path"},
                        "old_string": {"type": "string", "description": "Text to find"},
                        "new_string": {"type": "string", "description": "Replacement text"}
                    },
                    "required": ["path", "old_string", "new_string"]
                }
            },
            handler=lambda args, **kw: cached_patch(
                args.get("path", ""),
                args.get("old_string", ""),
                args.get("new_string", "")
            ),
            emoji="⚡"
        )

    except Exception:
        pass


# ─── 4. Transparent monkey-patching ──────────────────────────

def _patch_write_file():
    """Monkey-patch Hermes' write_file to use cached_write when content matches."""
    try:
        from tools.registry import registry

        handler_map = registry._handler_map if hasattr(registry, '_handler_map') else {}
        for name, reg in handler_map.items():
            if name == "write_file" or getattr(reg.get('schema', {}), 'get', lambda k, d=None: d)('name', '') == 'write_file':
                original = reg.get('handler')
                if original:
                    def make_wrapper(orig):
                        def wrapper(args, **kw):
                            path = args.get("path", "")
                            content = args.get("content", "")
                            if path and content is not None and cached_write is not None:
                                result = cached_write(path, content)
                                if result.get("unchanged"):
                                    return f"=== unchanged (content identical, write skipped) ==="
                                if "error" not in result:
                                    return result
                            # Fall through to original on any error
                            return orig(args, **kw)
                        return wrapper
                    reg['handler'] = make_wrapper(original)
                break
    except Exception:
        pass


def _patch_patch_tool():
    """Monkey-patch Hermes' patch tool to use cached_patch for the common single-replace case."""
    try:
        from tools.registry import registry

        handler_map = registry._handler_map if hasattr(registry, '_handler_map') else {}
        for name, reg in handler_map.items():
            if name == "patch" or getattr(reg.get('schema', {}), 'get', lambda k, d=None: d)('name', '') == 'patch':
                original = reg.get('handler')
                if original:
                    def make_wrapper(orig):
                        def wrapper(args, **kw):
                            path = args.get("path", "")
                            old_string = args.get("old_string", "")
                            new_string = args.get("new_string", "")
                            if path and old_string and new_string is not None and cached_patch is not None:
                                result = cached_patch(path, old_string, new_string)
                                if result.get("unchanged"):
                                    reason = result.get("reason", "skipped")
                                    return f"=== unchanged ({reason}) ==="
                                if "error" not in result:
                                    return result
                            return orig(args, **kw)
                        return wrapper
                    reg['handler'] = make_wrapper(original)
                break
    except Exception:
        pass


# ─── 5. Run on load ──────────────────────────────────────────

if TOOLRECALL_AVAILABLE:
    mode = _get_cache_mode()
    tools_registered = ["cached_read", "cached_terminal", "cached_write", "cached_patch"]

    _register_tools()

    if mode == "write":
        _patch_write_file()
        _patch_patch_tool()
        tools_registered.append("+transparent write_file/patch")

    daemon_active = daemon_running() if daemon_running else False

    print(f"  {'='*48}")
    print(f"  ToolRecall Caching Registered")
    print(f"  Tools: {', '.join(tools_registered)}")
    print(f"  Mode:  {'Write (monkey-patched)' if mode == 'write' else 'Separate'}")
    if daemon_active:
        print(f"  Backend: Daemon (UDS) — shared cache")
    else:
        print(f"  Backend: Direct SQLite — no daemon")
    print(f"  {'='*48}")
    print()
else:
    print("  ToolRecall not installed. Run: pip install toolrecall")
    print()
