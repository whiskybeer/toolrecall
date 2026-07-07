# ToolRecall Hermes Init — registers cached_* tools via Daemon Client.
#
# This init script registers cached_read, cached_terminal, cached_write,
# and cached_patch as explicit, separate tools in the Hermes Tool Registry.
# The tools forward requests to the ToolRecall Daemon (UDS) — or use direct
# SQLite as a fallback.
#
# In "transparent" mode (config: [hermes] transparent_cache = "transparent"),
# it also monkey-patches Hermes' built-in read_file, terminal, write_file,
# and patch tools so native tool calls are served from cache automatically.
# The agent never notices — it calls the same tool names.
#
# IMPORTANT: The transparent monkey-patches use a **deferred retry loop**
# because Hermes' tool registry is empty at init_scripts time. The patches
# retry every 300ms for up to 3s until the target tool appears. This ensures
# they fire BEFORE the first user turn without blocking session startup.
#
# Installation:
#   bash <(curl -s https://raw.githubusercontent.com/whiskybeer/toolrecall/main/setup.sh)
#
# Or manually:
#   pip install toolrecall
#   hermes config set agent.init_scripts '["~/.toolrecall/hermes_init.py"]'
#
# Then restart Hermes or run /reset.

import os
import threading

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
        os.path.expanduser("~/.config/toolrecall/toolrecall.toml")
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


# ─── Helper: deferred retry ──────────────────────────────────

def _deferred_patch(tool_name: str, patcher_fn):
    """Retry patching until the tool appears in the registry.

    Runs in a background thread. Retries every 300ms for up to 3s.
    After that, silently gives up — the session still works, just uncached.
    """
    import time
    for attempt in range(10):
        try:
            from tools.registry import registry
            entry = registry.get_entry(tool_name)
            if entry is not None:
                patcher_fn(entry)
                return
        except ImportError:
            pass  # registry module not yet available
        time.sleep(0.3)


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


# ─── 4. Transparent monkey-patching (deferred) ───────────────

def _patch_read_file(entry):
    """Wrap the read_file handler with cached_read."""
    original = entry.handler
    def make_wrapper(orig):
        def wrapper(args, **kw):
            path = args.get("path", "")
            if path and cached_read is not None:
                abs_path = os.path.abspath(path)
                result = cached_read(abs_path)
                if result and "error" not in result:
                    return result
            return orig(args, **kw)
        return wrapper
    entry.handler = make_wrapper(original)


def _patch_terminal(entry):
    """Wrap the terminal handler with cached_terminal."""
    original = entry.handler
    def make_wrapper(orig):
        def wrapper(args, **kw):
            command = args.get("command", "")
            if command and cached_terminal is not None:
                result = cached_terminal(command)
                if result and "error" not in result:
                    return result
            return orig(args, **kw)
        return wrapper
    entry.handler = make_wrapper(original)


def _patch_write_file(entry):
    """Wrap the write_file handler with cached_write (skip on unchanged)."""
    original = entry.handler
    def make_wrapper(orig):
        def wrapper(args, **kw):
            path = args.get("path", "")
            content = args.get("content", "")
            if path and content and cached_write is not None:
                abs_path = os.path.abspath(path)
                result = cached_write(abs_path, content)
                if result.get("unchanged"):
                    return "=== unchanged (content identical, write skipped) ==="
                if "error" not in result:
                    return result
            return orig(args, **kw)
        return wrapper
    entry.handler = make_wrapper(original)


def _patch_patch_tool(entry):
    """Wrap the patch handler with cached_patch."""
    original = entry.handler
    def make_wrapper(orig):
        def wrapper(args, **kw):
            path = args.get("path", "")
            old_string = args.get("old_string", "")
            new_string = args.get("new_string", "")
            if path and old_string and new_string is not None and cached_patch is not None:
                abs_path = os.path.abspath(path)
                result = cached_patch(abs_path, old_string, new_string)
                if result.get("unchanged"):
                    reason = result.get("reason", "skipped")
                    return f"=== unchanged ({reason}) ==="
                if "error" not in result:
                    return result
            return orig(args, **kw)
        return wrapper
    entry.handler = make_wrapper(original)


def _patch_search_files(entry):
    """Wrap the search_files handler with cached_terminal."""
    original = entry.handler
    def make_wrapper(orig):
        def wrapper(args, **kw):
            pattern = args.get("pattern", "")
            search_path = args.get("path", "")
            if pattern and cached_terminal is not None:
                cmd = f"search_files pattern={pattern} path={search_path}"
                result = cached_terminal(cmd, ttl=60)
                if result and "error" not in result:
                    return result
            return orig(args, **kw)
        return wrapper
    entry.handler = make_wrapper(original)


# ─── 5. Run on load ──────────────────────────────────────────

if TOOLRECALL_AVAILABLE:
    mode = _get_cache_mode()
    tools_registered = ["cached_read", "cached_terminal", "cached_write", "cached_patch"]

    _register_tools()

    if mode in ("transparent", "write"):
        # Deferred patching via background threads — retries until tools appear
        patch_targets = [
            ("read_file", _patch_read_file),
            ("terminal", _patch_terminal),
            ("write_file", _patch_write_file),
            ("patch", _patch_patch_tool),
            ("search_files", _patch_search_files),
        ]
        for name, fn in patch_targets:
            t = threading.Thread(
                target=_deferred_patch,
                args=(name, fn),
                daemon=True,
            )
            t.start()
        tools_registered.append("+transparent read_file/terminal/write_file/patch/search_files")

    print(f"  {'='*48}")
    print("  ToolRecall Caching Registered")
    print(f"  Tools: {', '.join(tools_registered)}")
    print(f"  Mode:  {'Transparent' if mode in ('transparent', 'write') else 'Separate'}")
    if daemon_running():
        print("  Backend: Daemon (UDS) — shared cache")
    else:
        print("  Backend: Direct SQLite — no daemon")
    print(f"  {'='*48}")
    print()
else:
    print("  ToolRecall not installed. Run: pip install toolrecall")
    print()
