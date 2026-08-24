"""ToolRecall CLI -- toolrecall status, stats, invalidate, index, serve, debug, nginx, mcp, daemon.

Usage:
    toolrecall status          # Show cache status
    toolrecall stats           # Detailed statistics (JSON)
    toolrecall invalidate      # Clear cache
    toolrecall reset-stats     # Reset statistics counters
    toolrecall index           # Index knowledge base
    toolrecall index-memory    # Index agent memory stores
    toolrecall index-dir       # Index a directory into knowledge DB
    toolrecall config-set      # Set a config value (section.key = value)
    toolrecall serve           # Start forward proxy (cache API responses)
    toolrecall debug           # Start debug/demo server (test cached_read via curl)
    toolrecall nginx           # Generate nginx config
    toolrecall mcp             # Start MCP Bridge (stdio → Daemon)
    toolrecall daemon          # Start/stop/manage cache daemon
    toolrecall shim            # Install/uninstall transparent cache shim (.pth)
    toolrecall init            # Create default config.toml and .env
"""

import os
import sys


def cmd_init():
    """Create boilerplate config and .env for users with interactive setup."""
    import os
    import sys

    cfg_dir = os.path.expanduser("~/.config/toolrecall")
    os.makedirs(cfg_dir, exist_ok=True)

    cfg_path = os.path.join(cfg_dir, "toolrecall.toml")
    env_path = os.path.join(cfg_dir, ".env")

    # ─── Security banner ───────────────────────────────
    print("╔══════════════════════════════════════════════════════════╗")
    print("║  ⚠️  ToolRecall Security Setup                          ║")
    print("║                                                          ║")
    print("║  ToolRecall uses DEFAULT-DENY path access control.       ║")
    print("║  The agent can ONLY read files under directories you     ║")
    print("║  explicitly allow below.                                ║")
    print("║                                                          ║")
    print("║  ⚠️  IMPORTANT — Consequences of allowing a path:        ║")
    print("║   • Every file under that path becomes readable          ║")
    print("║     through ToolRecall's MCP layer.                      ║")
    print("║   • If the agent is prompt-injected, files under         ║")
    print("║     allowed paths could be exfiltrated.                  ║")
    print("║   • Credential files (.env, .ssh/, .pem, .gitconfig)    ║")
    print("║     are still blocked inside allowed paths.              ║")
    print("║                                                          ║")
    print("║  Best practice: only add directories the agent needs.    ║")
    print("╚══════════════════════════════════════════════════════════╝")
    print()

    # ─── Interactive path collection (fallback to default if non-TTY) ─
    default_paths = ["/tmp", "~/.toolrecall"]
    paths = []

    if not sys.stdin.isatty():
        # Non-TTY (e.g. CI, Docker, pipe) — use defaults silently
        paths = list(default_paths)
        print("📄 Non-interactive shell detected — using default allowed paths.")
        print(f"   Allowed: {', '.join(default_paths)}")
        print("   Edit config.toml later to add more paths.")
        print()
    else:
        print("Enter the directories your agent should be able to read.")
        print("One path per line. Empty line when done.")
        print(f"Default (press Enter): {', '.join(default_paths)}")
        print()
        print("  ⚠️  Home directory (~/) is NOT in the default allowlist.")
        print("     Add only what the agent needs — keep everything else off-limits.")
        print()

        first = True
        while True:
            prompt = "Path 1: " if first else f"Path {len(paths) + 1}: "
            user_input = input(prompt).strip()
            first = False
            if not user_input:
                if not paths:
                    paths = list(default_paths)
                    print(f"  → Using defaults: {', '.join(default_paths)}")
                break
            expanded = os.path.expanduser(user_input)
            if not os.path.isdir(expanded):
                print(f"  ⚠️  Directory does not exist: {expanded}")
                yn = input("  Add anyway? [y/N] ").strip().lower()
                if yn != "y":
                    continue
            paths.append(user_input)

        print()

    # ─── Cache key normalization toggle ──────────────────
    print()
    print("╔══════════════════════════════════════════════════════════╗")
    print("║  🔑 Cache Key Normalization (Track 1 — Semantic Intent) ║")
    print("║                                                          ║")
    print("║  Normalizes tool call arguments before cache key         ║")
    print("║  generation so that semantically identical calls         ║")
    print("║  produce the same cache key:                            ║")
    print("║                                                          ║")
    print("║  • Sorts JSON keys — {b:2, a:1} == {a:1, b:2}          ║")
    print('║  • Strips whitespace — "  /tmp  " == "/tmp"            ║')
    print("║  • Removes noise — timestamps, session IDs, trace IDs   ║")
    print('║  • Lowercases command names — "LS -la" == "ls -la"     ║')
    print("║                                                          ║")
    print("║  ⚠️  Changes existing cache keys — existing entries      ║")
    print("║     become orphans until they expire or are overwritten. ║")
    print("╚══════════════════════════════════════════════════════════╝")
    print()

    norm_enabled = False
    if sys.stdin.isatty():
        resp = input("Enable cache key normalization? [y/N] ").strip().lower()
        norm_enabled = resp == "y"
        if norm_enabled:
            print("  ✅ Normalization enabled — broader cache hits across rephrased queries.")
        else:
            print("  ℹ️  Normalization disabled (default) — can be enabled later via config.")
        print()
    else:
        print("  ℹ️  Non-interactive — normalization disabled (default).")
        print("     Set [norm].enabled = true in config.toml to enable.")
        print()

    norm_toml = f"""
[norm]
# Cache key normalization — deterministic JSON + noise stripping.
# When enabled, tool call arguments are normalized before cache key
# generation: keys sorted, whitespace stripped, non-semantic fields
# (timestamps, session IDs, request IDs, trace IDs, nonces) removed.
# ⚠️ Changing this invalidates existing cache entries.
enabled = {"true" if norm_enabled else "false"}
"""

    # ─── Build config content ──────────────────────────
    paths_toml = ",\n    ".join(f'"{p}"' for p in paths)

    cfg_content = f"""# ToolRecall Configuration
# Created by `toolrecall init`

[storage]
backend = "sqlite"

[cache]
file_ttl = -1          # read_file: until file modification
skill_ttl = -1         # skill_view: until skill update
terminal_default_ttl = 300

[security]
tool_access_control = false
dangerous_tool_keywords = []

[mcp]
# ⚠️ SECURITY: Default-deny file access control.
# The agent can ONLY read files under these directories.
# Add paths the agent needs — nothing more.
# Credential files (.env, .ssh/, .pem) are still blocked inside
# allowed paths as a secondary safety net.
allowed_paths = [
    {paths_toml}
]
allow_terminal = true
allow_invalidate = false

[mcp_multiplex]
enabled = true
default_ttl = 60
# Server names: auto-resolved via built-in registry.
# Built-in (stdlib, no deps): time, github, sequential-thinking
# External (needs uvx): fetch, filesystem, git, memory, brave-search, playwright, slack
servers = ["time", "sequential-thinking"]
idle_minutes = 15

# Custom server overrides (optional — auto-resolve is the default).
# Uncomment to override auto-resolved servers or add custom ones:
# [mcp_multiplex.servers_config]
# github = {{ command = "npx", args = ["-y", "@modelcontextprotocol/server-github"] }}
# fetch = {{ command = "uvx", args = ["mcp-server-fetch"] }}

[forward_proxy]
# ToolRecall Daemon starts the forward proxy on :8569 automatically.
# Set TOOLRECALL_FORWARD_PORT to change the default port.
# Point your API client at http://localhost:8569 to get cached API responses.
{norm_toml}"""

    env_content = """# ToolRecall Secrets
# Loaded safely by the Daemon. Do NOT commit this file.
# Example for GitHub MCP:
GITHUB_PERSONAL_ACCESS_TOKEN=""
"""

    created_cfg = False
    if not os.path.exists(cfg_path):
        with open(cfg_path, "w") as f:
            f.write(cfg_content)
        created_cfg = True
    else:
        print("ℹ️  config.toml already exists — not overwriting.")
        print("   Run `toolrecall config-set mcp.allowed_paths [...]` to update paths.")
        print()

    created_env = False
    if not os.path.exists(env_path):
        with open(env_path, "w") as f:
            f.write(env_content)
        os.chmod(env_path, 0o600)
        created_env = True

    print(f"ToolRecall directory: {cfg_dir}")
    if created_cfg:
        print("✅ Created config.toml")
        print(f"   Allowed paths: {', '.join(paths)}")
    if created_env:
        print("✅ Created .env (API keys — keep secret!)")

    print()
    print("Next steps:")
    print(f"  1. Edit {env_path} to add your API keys (if needed)")


def cmd_status():
    """Show cache status via daemon or directly (always includes recent activity table)."""
    from toolrecall.cache import get_stats

    stats = get_stats()
    print("=" * 50)
    print("  ToolRecall Cache Status")
    print("=" * 50)
    for k, v in stats.items():
        if k == "recent":
            continue
        if isinstance(v, dict):
            saved = v.get("tokens_not_read_from_disk", 0)
            adjusted = v.get("tokens_not_read_from_disk_adjusted", 0)
            read = v.get("tokens_read_from_disk", 0)
            context = v.get("context_tokens_saved", 0)
            content_tokens = v.get("cached_content_tokens", 0)
            saved_str = f", tokens_not_read_from_disk={saved:,}" if saved else ""
            adjusted_str = f", adjusted={adjusted:,}" if adjusted and adjusted != saved else ""
            read_str = f", tokens_read_from_disk={read:,}" if read else ""
            context_str = f", context_tokens_saved={context:,}" if context else ""
            content_str = f", cached_content_tokens={content_tokens:,}" if content_tokens else ""
            print(
                f"  {k}: {v['hits']} hits, {v['misses']} misses, "
                f"hit_rate={v['hit_rate']}{read_str}{saved_str}{adjusted_str}{context_str}{content_str}"
            )
        else:
            print(f"  {k}: {v}")
    # Recent activity
    recent = stats.get("recent", [])
    if recent:
        print()
        print("  ── Last 20 accesses ──")
        print(f"  {'ago':>8} {'type':<12} {'tokens':>8} {'cached_at':<26} {'path'}")
        print(f"  {'─' * 8} {'─' * 12} {'─' * 8} {'─' * 26} {'─' * 40}")
        for r in recent:
            icon = "✅" if r["hit"] else "⬇️"
            p = r.get("path", r["category"])
            if len(p) > 40:
                p = "..." + p[-37:]
            tokens_str = f"{r['tokens']:,}" if r.get("tokens", 0) else "-"
            ca = r.get("cached_at", "")[11:19]  # HH:MM:SS from ISO
            print(f"  {r['since_status']:>8} {icon} {tokens_str:>8} {ca:<26} {p}")

    # ─── Terminal cache info ─────────────────
    print()
    print("  Terminal cache (read-only commands):")
    print("    System: hostname(3600s), whoami(3600s), pwd(3600s), uptime(300s), uname(3600s)")
    print("    Files:  ls(60s), cat(30s), head(30s), tail(30s), wc(30s)")
    print("    Search: grep(60s), rg(60s), find(60s), fd(60s)")
    print("    Git:    status(30s), diff(30s), log(30s), branch(300s), stash(300s)")
    print("    Env:    which(3600s), python3 --version(3600s), pip list(600s)")
    print()
    print("  ⚠️  Only read-only commands are cached. Dangerous")
    print("     commands (rm, sudo, git push, kill) are NEVER cached.")
    print()


def cmd_stats():
    """Detailed statistics as JSON."""
    from toolrecall.cache import get_stats
    import json as _json

    stats = get_stats()
    print(_json.dumps(stats, indent=2))


def cmd_invalidate():
    """Clear cache via Daemon or direct SQLite fallback."""
    try:
        from toolrecall.client import cache_invalidate

        print(cache_invalidate())
    except Exception:
        from toolrecall.cache import invalidate_all

        invalidate_all()
        print("ToolRecall cache cleared (direct).")


def cmd_reset_stats():
    """Reset cache statistics counters (hits, misses, tokens_read_from_disk) without clearing cache entries."""
    from toolrecall.cache import reset_stats

    reset_stats()
    print("Cache statistics reset (hits/misses/tokens). Cache entries preserved.")


def cmd_index():
    """Index knowledge base. Use --memory to also index agent memory stores."""
    from toolrecall.docs import index_all, index_agent_memory

    print("Indexing knowledge database...")
    total = index_all()
    print(f"Done. {total} pages indexed.")

    if "--memory" in sys.argv:
        print("Indexing agent memory stores...")
        mem_total = index_agent_memory()
        print(f"Done. {mem_total} memory entries indexed.")


def cmd_index_memory():
    """Index agent persistent memory stores (MEMORY.md, USER.md) into knowledge DB.

    Uses AGENT_HOME env var (or HERMES_HOME for backward compat) to locate
    the memories/ directory.
    """
    from toolrecall.docs import index_agent_memory

    # Optional: custom source label via --source
    source = "agent-memory"
    if "--source" in sys.argv:
        idx = sys.argv.index("--source")
        if idx + 1 < len(sys.argv):
            source = sys.argv[idx + 1]

    print(f"Indexing agent memory stores (source='{source}')...")
    total = index_agent_memory(source=source)
    print(f"Done. {total} memory entries indexed with FTS5 (source='{source}').")
    print()
    print("Query via: docs_search('<query>', source='<source>')")
    print("Or via MCP: toolrecall docs_search '<query>'")
    print()


def cmd_index_dir():
    r"""Index a directory into the knowledge database.

    Usage:
        toolrecall index-dir ~/Documents/Obsidian\ Vault
        toolrecall index-dir --source my-notes ~/notes
    """
    from toolrecall.docs import index_directory

    # Parse args
    args = [a for a in sys.argv[2:] if not a.startswith("--source")]
    source_override = None
    if "--source" in sys.argv:
        idx = sys.argv.index("--source")
        if idx + 1 < len(sys.argv):
            source_override = sys.argv[idx + 1]

    if not args:
        print("Usage: toolrecall index-dir [--source label] <directory> [directory2 ...]")
        print()
        print("Index all .md files from the given directory into the FTS5 knowledge DB.")
        print("Each file becomes a searchable page. Use --source to set a custom label")
        print("(default: basename of the directory).")
        print()
        print("Examples:")
        print("  toolrecall index-dir ~/Documents/Obsidian\\\\ Vault")
        print("  toolrecall index-dir --source my-wiki ~/wiki")
        print("  toolrecall index-dir ~/notes ~/Documents/Obsidian\\\\ Vault")
        return

    total_all = 0
    for dir_arg in args:
        dir_path = os.path.expanduser(dir_arg)
        if not os.path.isdir(dir_path):
            print(f"⚠️  Not a directory: {dir_path}")
            continue

        source = source_override or os.path.basename(dir_path)
        print(f"Indexing '{dir_path}' as source='{source}'...")
        count = index_directory(dir_path, source=source)
        print(f"  → {count} pages indexed")
        total_all += count

    print(f"\nDone. {total_all} total pages indexed.")
    print("Query via: docs_search('<query>', source='<source>')")


def cmd_config_set():
    """Set a config value in config.toml.

    Usage:
        toolrecall config-set proxy.port 9090
        toolrecall config-set mcp.allow_terminal true
        toolrecall config-set mcp.allowed_paths "['/data', '/projects']"
    """
    from toolrecall.config import load_config, save_config

    # save_config uses built-in TOML serializer — no external deps

    args = sys.argv[2:]
    if len(args) < 2 or "--help" in args or "-h" in args:
        print("Usage: toolrecall config-set <section.key> <value>")
        print()
        print("Examples:")
        print("  toolrecall config-set proxy.port 9090")
        print("  toolrecall config-set mcp.allow_terminal true")
        print(
            "  toolrecall config-set security.tool_access_control true  # MCP keyword access control (not OS sandbox)"
        )
        print("  toolrecall config-set mcp.allowed_paths \"['/data', '/projects']\"")
        return

    key = args[0]
    val = args[1]
    parts = key.rsplit(".", 1)

    if len(parts) != 2:
        print(f"❌ Invalid key: '{key}'. Use section.key format (e.g. proxy.port)")
        return

    section, name = parts
    cfg_path = os.path.expanduser("~/.config/toolrecall/toolrecall.toml")
    cfg = load_config(cfg_path)

    # Parse value
    parsed_val = val
    if val.lower() == "true":
        parsed_val = True
    elif val.lower() == "false":
        parsed_val = False
    else:
        try:
            parsed_val = int(val)
        except ValueError:
            try:
                parsed_val = float(val)
            except ValueError:
                # Try as list
                if val.startswith("[") and val.endswith("]"):
                    import ast

                    try:
                        parsed_val = ast.literal_eval(val)
                    except Exception:
                        pass
                # Keep as string

    # Apply
    if section not in cfg._data:
        cfg._data[section] = {}
    cfg._data[section][name] = parsed_val

    if save_config(cfg_path, cfg):
        print(f"✅ Set {key} = {parsed_val!r} in {cfg_path}")
        print("⚠️  Restart the daemon for changes to take effect.")
    else:
        print(f"❌ Failed to write {cfg_path}")


def cmd_serve():
    """Start the forward proxy (caches LLM API responses).

    Note: The daemon already starts the forward proxy automatically.
    This command is only needed if you want a standalone instance
    on a different port while the daemon is NOT running.
    """
    # Parse --port from argv
    port_override = None
    clean_argv = []
    i = 0
    while i < len(sys.argv):
        if sys.argv[i] == "--port" and i + 1 < len(sys.argv):
            port_override = int(sys.argv[i + 1])
            i += 2
        elif sys.argv[i].startswith("--port="):
            port_override = int(sys.argv[i].split("=", 1)[1])
            i += 1
        else:
            clean_argv.append(sys.argv[i])
            i += 1
    sys.argv = clean_argv

    if "--help" in sys.argv or "-h" in sys.argv:
        print("Usage: toolrecall serve [--port PORT]")
        print()
        print("Start the forward proxy — caches LLM API responses (OpenAI, Anthropic, etc.)")
        print("before they leave your machine. On cache hit, returns cached response")
        print("without contacting the provider.")
        print()
        print("Note: The forward proxy also starts automatically with `toolrecall daemon`.")
        print("Use `toolrecall serve` only if you need a standalone instance on a different port.")
        print()
        print("Options:")
        print("  --help, -h          Show this help message")
        print("  --port PORT         Override forward proxy port")
        print()
        print(f"Default port: {int(os.environ.get('TOOLRECALL_FORWARD_PORT', '8569'))}")
        print()
        print("Examples:")
        print("  toolrecall serve                    # Forward proxy on :8569")
        print("  toolrecall serve --port 9090        # Forward proxy on :9090")
        print()
        print("Use with:")
        print("  export OPENAI_BASE_URL=http://localhost:8569")
        print("  export ANTHROPIC_BASE_URL=http://localhost:8569")
        return

    # Check if daemon is already running (it starts the proxy on default port)
    from toolrecall.transport import TransportClient

    daemon_check = TransportClient()
    daemon_ping = daemon_check.send({"cmd": "ping"})
    daemon_running = daemon_ping.get("pong", False)

    if daemon_running and (
        port_override is None
        or port_override == int(os.environ.get("TOOLRECALL_FORWARD_PORT", "8569"))
    ):
        print("The daemon already manages the forward proxy on port 8569.")
        print("To run a standalone proxy on a different port, use:")
        print("  toolrecall serve --port <PORT>")
        print("while the daemon is stopped.")
        return

    from toolrecall.proxy import run_forward_proxy

    port = (
        port_override
        if port_override is not None
        else int(os.environ.get("TOOLRECALL_FORWARD_PORT", "8569"))
    )
    run_forward_proxy(port=port)


def cmd_mcp():
    """MCP Bridge & Registry commands.

    Subcommands:
      list    List registered and active MCP servers
      <none>  Start MCP Bridge (stdio -> Daemon)
    """
    if len(sys.argv) >= 3:
        sub = sys.argv[2]
        if sub == "list":
            return cmd_mcp_list()
        elif sub == "--help" or sub == "-h":
            pass  # fall through to help

    # Default: start MCP Bridge
    from toolrecall.mcp_bridge import main as bridge_main

    bridge_main()


def cmd_mcp_list():
    """List registered MCP servers with status."""
    from toolrecall.mcp_registry import list_registered_servers, has_uvx

    servers = list_registered_servers()
    if not servers:
        print("No MCP servers registered.")
        return

    print(f"MCP Server Registry  ({len(servers)} total)")
    print(f"{'Name':<25} {'Source':<10} {'Command':<30} {'Args'}")
    print("-" * 100)
    for srv in servers:
        source = srv["source"]
        cmd = srv["command"]
        args = " ".join(srv["args"])
        print(f"{srv['name']:<25} {source:<10} {cmd:<30} {args}")

    # Check uvx
    if not has_uvx():
        print()
        print("⚠️  uvx not found on PATH — external servers (fetch, filesystem, git, ...)")
        print("   will NOT start until uvx is installed.")
        print("   Install: curl -LsSf https://astral.sh/uv/install.sh | sh")

    print()
    print("Active via daemon:")
    print("  Run `toolrecall status` or connect MCP Bridge to see live servers.")


def cmd_debug():
    """Start minimal debug/demo server on :8570."""
    from toolrecall.proxy import run_debug_server

    port_override = None
    for i, arg in enumerate(sys.argv[2:], 2):
        if arg == "--port" and i + 1 < len(sys.argv):
            port_override = int(sys.argv[i + 1])
        elif arg.startswith("--port="):
            port_override = int(arg.split("=", 1)[1])

    if "--help" in sys.argv or "-h" in sys.argv:
        print("Usage: toolrecall debug [--port PORT]")
        print()
        print("Start minimal debug/demo server.")
        print("Endpoints:")
        print("  GET /read?path=X   cached_read demo")
        print("  GET /term?cmd=X    cached_terminal demo")
        print("  GET /stats         cache statistics")
        print("  GET /health        daemon status")
        print()
        print("Example:")
        print("  time curl http://localhost:8570/read?path=README.md")
        return

    run_debug_server(port=port_override or 8570)


def _ensure_daemon():
    """Auto-start the ToolRecall cache daemon if not running.

    Tries (in order):
    1. systemd --user (Linux with systemd)
    2. Direct fork + run_daemon() (Docker, macOS, Codespaces)
    3. Windows fallback (subprocess.DETACHED_PROCESS)

    Returns True if daemon is running after attempt, False otherwise.

    Note: auto-starts the daemon silently (output goes to DEVNULL). To start
    it explicitly with visible output, run ``toolrecall daemon`` separately.
    """
    from toolrecall.transport import TransportClient, DEFAULT_PATH
    import time

    # ── 1. Already running? ──
    try:
        tc = TransportClient(DEFAULT_PATH)
        resp = tc.send({"cmd": "ping"})
        if resp.get("pong"):
            return True
    except Exception:
        pass

    # ── 2. systemd user service ──
    import subprocess

    try:
        result = subprocess.run(
            ["systemctl", "--user", "start", "toolrecall-daemon"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode == 0:
            for _ in range(10):
                time.sleep(0.5)
                try:
                    tc = TransportClient(DEFAULT_PATH)
                    resp = tc.send({"cmd": "ping"})
                    if resp.get("pong"):
                        return True
                except Exception:
                    continue
    except FileNotFoundError:
        pass  # No systemd — fall through

    # ── 3. Direct subprocess (no systemd) ──
    # Use subprocess instead of fork to avoid venv-vs-system conflicts.
    # start_new_session=True detaches from the parent process group so the
    # daemon survives the CLI process exiting (e.g. when opencode ends a session).
    # Important: use the binary (toolrecall daemon --foreground) not python -m,
    # because toolrecall has no __main__.py and `python -m toolrecall` fails.
    import sys as _sys
    import subprocess as _sp
    import shutil as _shutil

    try:
        print(
            "[ToolRecall] Daemon not running — auto-starting silently in background...",
            file=_sys.stderr,
            flush=True,
        )
        _toolrecall_bin = _shutil.which("toolrecall")
        if not _toolrecall_bin:
            # Fallback: locate the installed package's cli module directly
            _toolrecall_bin = _sys.executable
            _sp.Popen(
                [
                    _toolrecall_bin,
                    "-c",
                    "from toolrecall.cli import cmd_daemon; import sys; sys.argv = ['toolrecall', 'daemon', '--foreground']; cmd_daemon()",
                ],
                stdout=_sp.DEVNULL,
                stderr=_sp.DEVNULL,
                start_new_session=True,
            )
        else:
            _sp.Popen(
                [_toolrecall_bin, "daemon", "--foreground"],
                stdout=_sp.DEVNULL,
                stderr=_sp.DEVNULL,
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

    # ── 4. Windows fallback ──
    import sys as _sys

    if _sys.platform == "win32":
        try:
            import subprocess as _sp

            _sp.Popen(
                ["toolrecall", "daemon", "--foreground"],
                creationflags=_sp.DETACHED_PROCESS,
                stdout=_sp.DEVNULL,
                stderr=_sp.DEVNULL,
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


def _ensure_shim():
    """Install OS-level shim if not present, then load it into the current process.

    Why this is needed: the tr_shim.pth file auto-imports toolrecall.shim
    on every Python process startup. The .pth file uses a bare
    'import toolrecall.shim' which relies on Python's normal import
    resolution — this works with both editable and regular installs.
    """
    import os
    import shutil
    import sys

    try:
        installed = False
        # Check if shim is already in site-packages
        for p in sys.path:
            if p.endswith("site-packages"):
                pth = os.path.join(p, "tr_shim.pth")
                if os.path.exists(pth):
                    installed = True
                    break

        if not installed:
            # Install the .pth file from the package's bundled copy
            for p in sys.path:
                if p.endswith("site-packages") and os.path.isdir(p):
                    pth_src = os.path.join(os.path.dirname(__file__), "tr_shim.pth")
                    pth_dst = os.path.join(p, "tr_shim.pth")
                    if os.path.exists(pth_src):
                        shutil.copy2(pth_src, pth_dst)
                        print("  ℹ️  Shim auto-installed (tr_shim.pth)")
                        installed = True
                    break

        # Load the shim into the CURRENT process so existing agents benefit immediately
        if installed:
            try:
                import toolrecall.shim

                toolrecall.shim.apply()  # Force apply patches (idempotent)
            except (ImportError, AttributeError):
                pass
    except Exception:
        pass  # Silently ignore — shim is optional


def cmd_daemon():
    """Manage the ToolRecall Cache Daemon.

    Starts cache daemon + MCP bridge + forward proxy (:8569)."""
    from toolrecall.daemon import run_daemon, stop_daemon, daemon_status

    if "--help" in sys.argv or "-h" in sys.argv:
        print("Usage: toolrecall daemon [--foreground] [--stop] [--status]")
        print()
        print("Start the ToolRecall cache daemon.")
        print("  • MCP bridge: agents connect via stdio MCP")
        print("  • Forward proxy: http://127.0.0.1:8569 (caches API responses)")
        print("  • Use --foreground to run in terminal (not daemonized)")
        print()
        print("Then point agents OPENAI_BASE_URL to http://localhost:8569")
        return

    # Accept both --stop (flag) and "stop" (positional subcommand)
    args = sys.argv[2:]  # everything after "toolrecall daemon"
    if "--stop" in args or "stop" in args:
        stop_daemon()
    elif "--status" in args or "status" in args:
        daemon_status()
    elif "--foreground" in args or "foreground" in args:
        run_daemon(foreground=True)
    else:
        run_daemon(foreground=False)


def cmd_nginx():
    """Generate nginx config."""
    cfg_dir = os.path.expanduser("~/.toolrecall")
    os.makedirs(cfg_dir, exist_ok=True)
    out_path = os.path.join(cfg_dir, "nginx-toolrecall.conf")

    nginx_cfg = """# ToolRecall — Nginx Reverse Proxy Config
# Generated by `toolrecall nginx`
# Place in /etc/nginx/sites-available/toolrecall
# Then: ln -s /etc/nginx/sites-available/toolrecall /etc/nginx/sites-enabled/
# Then: nginx -t && systemctl reload nginx

server {
    listen 80;
    server_name localhost;

    proxy_cache off;
    proxy_no_cache 1;
    proxy_cache_bypass 1;

    location /toolrecall/ {
        proxy_pass http://127.0.0.1:8569/;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_connect_timeout 5s;
        proxy_read_timeout 30s;
        proxy_send_timeout 30s;
    }

    # Optional: Password protection
    # location /toolrecall/ {
    #     auth_basic "ToolRecall";
    #     auth_basic_user_file /etc/nginx/.htpasswd_toolrecall;
    #     proxy_pass http://127.0.0.1:8569/;
    # }
}

# SSL Version (recommended)
# server {
#     listen 443 ssl;
#     server_name toolrecall.dev;
#     ssl_certificate /etc/letsencrypt/live/toolrecall.dev/fullchain.pem;
#     ssl_certificate_key /etc/letsencrypt/live/toolrecall.dev/privkey.pem;
#     location / {
#         proxy_pass http://127.0.0.1:8569/;
#     }
# }
"""
    with open(out_path, "w") as f:
        f.write(nginx_cfg)
    print(f"Nginx config written to: {out_path}")
    print()
    print("To install:")
    print(f"  sudo cp {out_path} /etc/nginx/sites-available/toolrecall")
    print("  sudo ln -s /etc/nginx/sites-available/toolrecall /etc/nginx/sites-enabled/")
    print("  sudo nginx -t && sudo systemctl reload nginx")


def cmd_shim():
    """Install/uninstall/inspect the transparent cache shim (.pth) agent-agnostically.

    Modes:
      toolrecall shim --install                current venv only (back-compat)
      toolrecall shim --install --venv <path>  target one venv (root or bin/python)
      toolrecall shim --install --all          discover + install into every venv
      toolrecall shim --status [--venv <path> | --all]
      toolrecall shim --uninstall [--venv <path> | --all]
      toolrecall shim --disable             persistent disable (marker file)
      toolrecall shim --enable              clear the persistent disable marker
    Any unrecognized flag prints usage and exits non-zero (no silent swallowing).
    """
    from toolrecall import venvs as venv_mod

    args = sys.argv[2:]

    def usage(exit_code: int = 1):
        print(
            "Usage: toolrecall shim [--install|--uninstall|--status|--disable|--enable] [--venv <path>|--all]"
        )
        print()
        print("  --install            Install .pth shim into a Python env")
        print("  --uninstall          Remove the .pth shim (leaves the package)")
        print("  --status             Report shim state")
        print("  --disable            Persistently disable the shim (marker file)")
        print("  --enable             Re-enable the shim (clear the disable marker)")
        print("  --venv <path>        Target a specific venv (root dir or its bin/python)")
        print("  --all                Apply to every discovered venv (excl. toolrecall's own)")
        print("  --yes / -y           Skip the opt-in confirmation prompt (default is NO)")
        print()
        print("Python-vs-bridge model:")
        print("  Python agents w/ own venv (Hermes, Codex, OpenCode): .pth shim in that venv")
        print("  Non-Python agents (Claude Code, Cursor, Cline): MCP bridge `toolrecall mcp`")
        sys.exit(exit_code)

    action = None
    target = None
    all_flag = False
    yes_flag = False
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("--install", "install"):
            action = "install"
        elif a in ("--uninstall", "uninstall"):
            action = "uninstall"
        elif a in ("--status", "status"):
            action = "status"
        elif a in ("--disable", "disable"):
            action = "disable"
        elif a in ("--enable", "enable"):
            action = "enable"
        elif a in ("--venv", "-v"):
            if i + 1 >= len(args):
                print("Error: --venv requires a path argument")
                usage(2)
            target = args[i + 1]
            i += 1
        elif a in ("--all", "-a"):
            all_flag = True
        elif a in ("--yes", "-y"):
            yes_flag = True
        elif a in ("-h", "--help"):
            usage(0)
        else:
            print(f"Error: unknown shim flag: {a}")
            usage(2)
        i += 1

    if action is None:
        action = "status"  # bare `toolrecall shim` → status (back-compat)

    if action in ("disable", "enable"):
        # Global marker operations — independent of any venv target.
        import toolrecall.shim as shim_mod

        if action == "disable":
            if shim_mod.disable():
                print("✅ Shim DISABLED (persistent).")
                print(
                    "   Existing processes keep running as-is; new Python processes skip the shim."
                )
                print("   Re-enable with: toolrecall shim --enable")
                print(f"   Marker: {shim_mod._marker_path()}")
            else:
                print(f"⚠️  Could not write disable marker: {shim_mod._marker_path()}")
        else:
            if shim_mod.enable():
                print("✅ Shim ENABLED.")
                print("   New Python processes will apply the shim again.")
            else:
                print("⚠️  Could not clear the disable marker.")
        return

    def make_venv(path: str):
        """Resolve `path` (venv root OR its bin/python) to a Venv, or None."""
        p = os.path.expanduser(path)
        if os.path.isdir(p) and os.path.isfile(os.path.join(p, "pyvenv.cfg")):
            # venv root
            launcher = None
            bindir = os.path.join(p, "bin")
            if os.path.isdir(bindir):
                for name in sorted(os.listdir(bindir)):
                    if name.startswith("python3"):
                        full = os.path.join(bindir, name)
                        if os.access(full, os.X_OK):
                            launcher = full
                            break
            if not launcher:
                print(f"Error: no python launcher found in {p}/bin")
                return None
            sp = venv_mod._site_packages_of(launcher)
            if not sp:
                print(f"Error: could not resolve site-packages for {launcher}")
                return None
            return venv_mod.Venv(root=p, python=launcher, site_packages=sp)
        if os.path.isfile(p) and os.path.basename(p).startswith("python"):
            # direct python path
            root = os.path.dirname(os.path.dirname(p))
            sp = venv_mod._site_packages_of(p)
            if not sp:
                print(f"Error: could not resolve site-packages for {p}")
                return None
            return venv_mod.Venv(root=root, python=p, site_packages=sp)
        print(f"Error: not a venv root or python path: {path}")
        return None

    current_env = None
    for p in sys.path:
        if p.endswith("site-packages") and os.path.isdir(p):
            # current interpreter's own venv
            py = sys.executable
            current_env = venv_mod.Venv(
                root=os.path.dirname(os.path.dirname(p)),
                python=py,
                site_packages=p,
            )
            break

    if target:
        v = make_venv(target)
        if v is None:
            sys.exit(2)
        targets = [v]
    elif all_flag:
        found = venv_mod.discover_python_venvs()
        if not found:
            print("No Python venvs discovered (other than toolrecall's own).")
            return
        print(f"Discovered {len(found)} venv(s):")
        for v in found:
            print(f"  {v.python}")
        targets = found
    else:
        if current_env is None:
            print("Error: could not find the current environment's site-packages")
            sys.exit(2)
        targets = [current_env]

    if action == "status":
        import toolrecall.shim as shim_mod

        if shim_mod._marker_disabled():
            print(f"⚠️  Shim globally DISABLED via marker: {shim_mod._marker_path()}")
            print("    Run `toolrecall shim --enable` to re-enable.\n")
        for v in targets:
            st = venv_mod.shim_status(v)
            ok = st["probe_ok"]
            pth = "present" if st["pth_present"] else "missing"
            pkg = "ok" if st["package_importable"] else "MISSING"
            icon = "✅" if ok else "❌"
            print(f"{icon} {v.root}")
            print(f"     python: {v.python}")
            print(f"     package: {pkg} | shim: {pth} | probe: {'pass' if ok else 'FAIL'}")
        return

    if action == "install":
        for v in targets:
            if not yes_flag and not os.environ.get("TOOLRECALL_NONINTERACTIVE"):
                if not _confirm_install(v):
                    print(f"  skipped (opt-out): {v.root}")
                    continue
            if venv_mod.ensure_shim(v):
                print(f"✅ Shim active in {v.root} (verified: import toolrecall.shim OK)")
            else:
                print(f"⚠️  Shim install FAILED in {v.root} — see diagnosis below")
                st = venv_mod.shim_status(v)
                if not st["package_importable"]:
                    print(
                        f"     `toolrecall` not importable in {v.python}; "
                        "install it (pip/uv) or check the venv is writable"
                    )
                elif not st["pth_present"]:
                    print(f"     could not write {v.site_packages}/tr_shim.pth")
                else:
                    print(f"     import probe failed in {v.python}")
        return

    if action == "uninstall":
        for v in targets:
            if venv_mod.uninstall_shim(v):
                print(f"✅ Shim removed from {v.root}")
            else:
                print(f"⚠️  Could not remove shim from {v.root}")


def _confirm_install(v) -> bool:
    """Show the opt-in prompt for shimming a Python venv. Default DISABLED (N)."""
    try:
        print()
        print(f"Detected Python agent venv: {v.root}")
        print("  ToolRecall's .pth shim patches open()/subprocess for EVERY python")
        print("  process in this venv for transparent caching.")
        print()
        print("  Risks:    broad interception of non-agent reads; stale-content caching;")
        print("            behavior changes if daemon is down.")
        print("  Benefits: zero-config cache, ~99% hit rate, terminal dedup, token savings.")
        print()
        resp = input("  Enable shim? [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    return resp in ("y", "yes")


SYSTEMD_SERVICE_CONTENT = """[Unit]
Description=ToolRecall Cache Daemon
After=network.target

[Service]
Type=simple
ExecStart=%s
Restart=on-failure
RestartSec=5s
# Kill the daemon quickly on stop — the daemon's accept() loop has a 1s
# poll timeout and exits cleanly within ~2s.  The default 90s timeout
# leaves a hung daemon blocking restarts for over a minute.
TimeoutStopSec=10

[Install]
WantedBy=default.target
"""


def cmd_setup():
    """One-shot setup: init config → install systemd service → ensure daemon + shim."""
    import os

    print("=" * 56)
    print("  ToolRecall Setup — one-time installation")
    print("=" * 56)
    print()

    steps_ok = []
    errors = []

    # ─── 1. Config / init ───────────────────────────
    cfg_path = os.path.expanduser("~/.config/toolrecall/toolrecall.toml")
    if not os.path.exists(cfg_path):
        print("📄 No config found — running 'toolrecall init'...")
        cmd_init()
        print()
    else:
        steps_ok.append("config: found")

    # ─── 2. Systemd user service (optional) ─────────
    import subprocess

    try:
        systemd_dir = os.path.expanduser("~/.config/systemd/user")
        service_path = os.path.join(systemd_dir, "toolrecall-daemon.service")
        os.makedirs(systemd_dir, exist_ok=True)

        toolrecall_bin = os.path.expanduser("~/.local/bin/toolrecall")
        if not os.path.exists(toolrecall_bin):
            import shutil

            toolrecall_bin = shutil.which("toolrecall") or toolrecall_bin
        service_content = SYSTEMD_SERVICE_CONTENT % (toolrecall_bin + " daemon --foreground")
        with open(service_path, "w") as f:
            f.write(service_content)

        subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True, timeout=10)
        subprocess.run(
            ["systemctl", "--user", "enable", "toolrecall-daemon"], capture_output=True, timeout=10
        )
        steps_ok.append("systemd service: written + enabled")
    except FileNotFoundError:
        pass  # No systemd — _ensure_daemon will use fork

    # ─── 3. Daemon (auto-start) ────────────
    if _ensure_daemon():
        steps_ok.append("daemon: running")
    else:
        errors.append("daemon could not be started")

    # ─── 4. Agent integration ────────────────
    agents = _ensure_agent_integration()
    found_agents = [k for k, v in agents.items() if v]
    if found_agents:
        steps_ok.append(f"agent integration: {', '.join(found_agents)}")
    else:
        steps_ok.append("agent integration: none detected")

    # ─── Summary ──────────────────────────────────
    print()
    for msg in steps_ok:
        print(f"  ✅ {msg}")
    for msg in errors:
        print(f"  ❌ {msg}")
    print()
    print("=" * 56)
    if errors:
        print(f"  ⚠️  Setup finished with {len(errors)} issue(s)")
    else:
        print("  ✅ Setup complete — ToolRecall is ready")
    if not found_agents:
        print()
        print(_MANUAL_AGENT_GUIDE)
    else:
        print()
        print("  ℹ️  ToolRecall MCP tools available on next agent restart")
        print("     Agents must be told to use cached_read/cached_terminal over native tools")
        print("     (ToolRecall instruction snippets appended to agent configs where applicable)")
        print()
        print("  📦 Cached terminal commands (read-only):")
        print("     System: hostname(3600), whoami(3600), pwd(3600), uptime(300), uname(3600)")
        print("     Files:  ls(60), cat(30), head(30), tail(30), wc(30)")
        print("     Search: grep(60), rg(60), find(60), fd(60)")
        print("     Git:    status(30), diff(30), log(30), branch(300), stash(300)")
        print(
            "     Env:    which(3600), python3 --version(3600), pip list(600), node --version(3600)"
        )
        print("     Disk:   du(120), df(120)")
        print()
        print("  ⚠️  Security: only read-only commands are cached. Dangerous commands")
        print("     (rm, sudo, mv, git push, kill) are NEVER cached. Use ttl=0 to bypass.")
    print("=" * 56)


def _ensure_agent_integration():
    """Auto-detect supported agents and wire up ToolRecall access.

    Python agents with their own venv (Hermes, Codex, OpenCode) get the
    transparent cache via a ``.pth`` shim dropped into that venv's
    site-packages. This is **opt-in and default-disabled**: installing a shim
    patches open()/subprocess for EVERY python process in the venv, so we only
    do it after an explicit yes (or ``--yes`` / ``TOOLRECALL_NONINTERACTIVE``
    with ``--yes``). The ✅ is only printed after a probe confirms
    ``import toolrecall.shim`` works in that venv from a neutral cwd.

    Non-Python agents use the MCP bridge instead:
      - Claude Code: prefers `claude mcp add` (automatic), falls back to
        writing ~/.claude.json directly.
      - OpenCode/Crush: writes ~/.opencode/opencode.jsonc.

    Returns dict with keys: 'hermes', 'opencode', 'claude' — bool per agent.
    """
    import os
    import subprocess
    import json
    import shutil

    result = {}

    # ─── Hermes Agent (and any Python agent in its own venv) ───────────────
    # The OS-level .pth shim is agent-agnostic; detection is just "is there a
    # Python env we could shim?". We only auto-install into the Hermes venv on
    # explicit opt-in. This REPLACES the old behavior that printed an
    # unverified "✅ active" just because the `hermes` binary existed on PATH.
    from toolrecall import venvs as venv_mod

    hermes_bin = shutil.which("hermes")
    if hermes_bin:
        # Locate the Hermes venv via generic discovery (by venv containing the
        # hermes launcher, or by matching the binary's own environment).
        target = None
        for v in venv_mod.discover_python_venvs():
            launcher_bin = os.path.join(os.path.dirname(v.python), "hermes")
            if os.path.isfile(launcher_bin):
                target = v
                break
        if target is None:
            # Fall back to the current interpreter if it has a `hermes` launcher
            cur_l = os.path.join(os.path.dirname(sys.executable), "hermes")
            if os.path.isfile(cur_l):
                for p in sys.path:
                    if p.endswith("site-packages") and os.path.isdir(p):
                        target = venv_mod.Venv(
                            root=os.path.dirname(os.path.dirname(p)),
                            python=sys.executable,
                            site_packages=p,
                        )
                        break

        if target is not None:
            yes = "--yes" in sys.argv
            interactive = "TOOLRECALL_NONINTERACTIVE" not in os.environ
            if yes:
                # Explicit --yes → auto-install with verification
                done = venv_mod.ensure_shim(target)
                result["hermes"] = done
                if done:
                    print(
                        f"  ✅ Hermes transparent cache active in {target.root} "
                        "(verified: import toolrecall.shim probe passed)"
                    )
                else:
                    print(f"  ⚠️  Hermes shim install FAILED in {target.root}")
                    print(
                        "     `toolrecall` may not be installed in that venv "
                        "or the venv is read-only."
                    )
            elif interactive:
                # Interactive without --yes → show opt-in prompt (default N)
                if not _confirm_install(target):
                    print("  ℹ️  Hermes detected but shim not enabled (opt-in, default off).")
                    result["hermes"] = False
                else:
                    done = venv_mod.ensure_shim(target)
                    result["hermes"] = done
                    if done:
                        print(
                            f"  ✅ Hermes shim active in {target.root} "
                            "(verified: import toolrecall.shim OK)"
                        )
                    else:
                        print(f"  ⚠️  Hermes shim install FAILED in {target.root}")
                        print(
                            "     `toolrecall` may not be installed in that venv "
                            "or the venv is read-only."
                        )
            else:
                # Non-interactive without --yes (e.g. `setup` in CI) → leave off
                result["hermes"] = False
        else:
            result["hermes"] = False

    # ─── OpenCode / Crush ────────────────────────────────────────────────
    OC_DIR = os.path.expanduser("~/.opencode")
    if os.path.isdir(OC_DIR):
        oc_config_path = os.path.join(OC_DIR, "opencode.jsonc")
        config = _prepare_opencode_config(oc_config_path)
        with open(oc_config_path, "w") as f:
            json.dump(config, f, indent=2)
        print(f"  ✅ OpenCode MCP config written (automatic): {oc_config_path}")
        result["opencode"] = True
    else:
        result["opencode"] = False

    # ─── Claude Code ────────────────────────────────
    claude_bin = shutil.which("claude")

    if claude_bin:
        # Prompt user: file caching via MCP was tested and costs 2.4× more
        # for Claude Code. Offer multiplexer-only mode.
        claude_multiplexer_only = False
        if "--yes" not in sys.argv and "TOOLRECALL_NONINTERACTIVE" not in os.environ:
            print()
            print("  ⚠️  Claude Code detected — IMPORTANT:")
            print("     ToolRecall's file caching via MCP was tested on Claude Code")
            print("     and showed a 2.4× cost increase. File caching only helps")
            print("     stateless agents (Hermes, Cline, ADK).")
            print()
            print("  Options:")
            print("    f) Full bridge — file cache + multiplexer + proxy (default)")
            print("    m) Multiplexer-only — no file/terminal/cache tools")
            print()
            try:
                claude_choice = input("  Install [F/m]: ").strip().lower()
                if claude_choice == "m":
                    claude_multiplexer_only = True
            except (EOFError, KeyboardInterrupt):
                pass  # Non-interactive → default to full
        # Automatic: uses Claude's own CLI to register the MCP server
        mcp_args = ["toolrecall", "mcp"]
        if claude_multiplexer_only:
            mcp_args.append("--multiplexer-only")
        try:
            r = subprocess.run(
                [claude_bin, "mcp", "add", "toolrecall", "-s", "user", "--"] + mcp_args,
                capture_output=True,
                text=True,
                timeout=15,
            )
            if r.returncode == 0:
                label = "multiplexer-only" if claude_multiplexer_only else "full"
                print(f"  ✅ Claude Code MCP server registered ({label} — via 'claude mcp add')")
                result["claude"] = True
            else:
                print(
                    f"  ⚠️  'claude mcp add' returned exit {r.returncode} — falling back to direct config"
                )
                if r.stderr.strip():
                    print(f"     stderr: {r.stderr.strip()[:200]}")
                _write_claude_json_config(multiplexer_only=claude_multiplexer_only)
                result["claude"] = True
        except FileNotFoundError:
            _write_claude_json_config(multiplexer_only=claude_multiplexer_only)
            result["claude"] = True
        except subprocess.TimeoutExpired:
            print("  ⚠️  'claude mcp add' timed out — falling back to direct config")
            _write_claude_json_config(multiplexer_only=claude_multiplexer_only)
            result["claude"] = True
    else:
        # Fallback: detect via ~/.claude.json
        claude_json = os.path.expanduser("~/.claude.json")
        if os.path.exists(claude_json):
            _write_claude_json_config()
            print(f"  ✅ Claude Code MCP config written (automatic): {claude_json}")
            result["claude"] = True

    # Instruction snippet: tell Claude to prefer cached tools
    claude_dotdir = os.path.expanduser("~/.claude/claude_dotfiles")
    claude_md = os.path.join(claude_dotdir, "claude.md")
    if result.get("claude") and not claude_multiplexer_only and not os.path.exists(claude_md):
        os.makedirs(claude_dotdir, exist_ok=True)
        try:
            with open(claude_md, "w") as f:
                f.write(_CLAUDE_INSTRUCTION_SNIPPET)
            print(f"  ✅ Claude Code instruction snippet written (automatic): {claude_md}")
        except (OSError, IOError) as e:
            print(f"  ⚠️  Could not write instruction snippet: {e}")
    elif result.get("claude") and os.path.exists(claude_md):
        try:
            with open(claude_md) as f:
                content = f.read()
                if "ToolRecall" not in content:
                    with open(claude_md, "a") as f2:
                        f2.write(_CLAUDE_INSTRUCTION_SNIPPET)
                    print(f"  ✅ Appended ToolRecall instruction to {claude_md}")
        except (OSError, IOError) as e:
            print(f"  ⚠️  Could not check/update instruction snippet: {e}")

    return result


_CLAUDE_INSTRUCTION_SNIPPET = """
## ToolRecall Cache
ToolRecall is installed. When reading files, use `cached_read` via MCP instead of `read_file`.
When running terminal commands, use `cached_terminal` instead of `terminal`.
"""


_MANUAL_AGENT_GUIDE = """
No supported agent detected on this machine.

ToolRecall works with any AI coding agent via MCP. To integrate manually:

  Hermes Agent:
    toolrecall shim --install
    → OS-level .pth shim patches open()/subprocess.run() in every Python process.
      No per-agent config needed.

  Claude Code:
    claude mcp add toolrecall -s user -- toolrecall mcp
    → Adds cached_read/cached_terminal as MCP tools

  OpenCode / Crush:
    Add to ~/.opencode/opencode.jsonc under "mcp" key:
    { "toolrecall": { "type": "local", "command": "toolrecall", "args": ["mcp"], "enabled": true } }

  Cursor:
    Add to .cursorrules: "Use cached_read for file reads, cached_terminal for terminal commands"

  Cline / Roo Code:
    Add to .clinerules: same instruction as Cursor

  Any MCP-compatible agent:
    Add an MCP server with command "toolrecall" and args ["mcp"]

After configuring your agent: make sure the ToolRecall daemon is running:
    toolrecall daemon
    toolrecall status
"""


def _write_claude_json_config(multiplexer_only: bool = False):
    """Write toolrecall MCP entry to ~/.claude.json.

    Reads existing config, merges toolrecall into mcpServers, writes back.
    Used as fallback when 'claude mcp add' CLI command is unavailable.
    """
    import os
    import json

    claude_json = os.path.expanduser("~/.claude.json")
    config = {}
    if os.path.exists(claude_json):
        try:
            with open(claude_json) as f:
                config = json.load(f)
        except (json.JSONDecodeError, Exception):
            config = {}

    if "mcpServers" not in config:
        config["mcpServers"] = {}
    args = ["mcp"]
    if multiplexer_only:
        args.append("--multiplexer-only")
    config["mcpServers"]["toolrecall"] = {
        "command": "toolrecall",
        "args": args,
    }

    with open(claude_json, "w") as f:
        json.dump(config, f, indent=2)


def _prepare_opencode_config(config_path):
    """Read existing opencode config or create fresh one with Crush format.

    Detects Crush (mcp key) vs classic opencode (mcpServers key) and
    writes the appropriate format.
    """
    import json

    config = {}
    if os.path.exists(config_path):
        try:
            with open(config_path) as f:
                content = f.read()
            import re

            content = re.sub(r"//.*$", "", content, flags=re.MULTILINE)
            content = re.sub(r",\s*([}\]])$", r"\1", content)
            config = json.loads(content) if content.strip() else {}
        except (json.JSONDecodeError, Exception):
            config = {}

    is_crush = "mcp" in config and isinstance(config["mcp"], dict)

    toolrecall_entry = {
        "type": "local",
        "command": "toolrecall",
        "args": ["mcp"],
        "enabled": True,
    }

    if is_crush:
        config["mcp"]["toolrecall"] = toolrecall_entry
    else:
        if "mcpServers" not in config:
            config["mcpServers"] = {}
        config["mcpServers"]["toolrecall"] = {
            "command": "toolrecall",
            "args": ["mcp"],
        }
        config.pop("mcp", None)

    if is_crush:
        config.pop("$schema", None)
        config.setdefault("$schema", "https://opencode.ai/config.json")

    return config


def _revert_proxy_wiring_in_text(path_hint: str, text: str) -> str:
    """Strip the exact :8569 base-URL override lines ToolRecall wires.

    Non-destructive: only removes lines that point at localhost/127.0.0.1:8569,
    leaving everything else (including real-host overrides) intact.
    """
    out = []
    for line in text.splitlines(keepends=True):
        if (
            ":" in line
            and "8569" in line
            and ("OPENAI_BASE_URL" in line or "ANTHROPIC_BASE_URL" in line or "base_url" in line)
            and ("localhost" in line or "127.0.0.1" in line)
        ):
            continue  # drop the ToolRecall proxy override
        out.append(line)
    return "".join(out)


_PROXY_WIRING_FILES = [
    os.path.expanduser("~/.bashrc"),
    os.path.expanduser("~/.profile"),
    os.path.expanduser("~/.hermes/config.yaml"),
]


def _revert_proxy_wiring() -> list:
    """Revert agent base-URL pointing at the ToolRecall forward proxy.

    Returns list of human-readable changes made. Backs up each touched file it
    actually edits (timestamped sibling), so the revert is reversible.
    """
    import shutil as _shutil
    import time as _time

    changed = []
    for path in _PROXY_WIRING_FILES:
        if not os.path.exists(path):
            continue
        try:
            with open(path) as f:
                text = f.read()
        except OSError:
            continue
        new_text = _revert_proxy_wiring_in_text(path, text)
        if new_text == text:
            continue
        backup = f"{path}.toolrecall-bak.{_time.strftime('%Y%m%d-%H%M%S')}"
        _shutil.copy2(path, backup)
        with open(path, "w") as f:
            f.write(new_text)
        changed.append(f"{path}  (backup: {backup})")
    return changed


def cmd_stop():
    """Stop the ToolRecall daemon and revert agent proxy base-URL wiring."""
    from toolrecall.daemon import stop_daemon

    print("=" * 56)
    print("  ToolRecall Stop")
    print("=" * 56)
    stop_daemon()
    print()
    print("  Reverting forward-proxy base-URL wiring (agents → direct provider)...")
    changed = _revert_proxy_wiring()
    if changed:
        for c in changed:
            print(f"    ⚙️  {c}")
    else:
        print("    ℹ️  No :8569 base-URL overrides found — nothing to revert.")
    print()
    print("✅ ToolRecall stopped. Agents now call their providers directly.")
    print("   Restart with: toolrecall daemon")


def cmd_restart():
    """Health check + restart via systemd: config check → systemctl --user restart → verify.
    Auto-installs OS-level shim if not present."""
    import os
    import subprocess
    import time

    print("=" * 56)
    print("  ToolRecall Restart — health check + systemd restart")
    print("=" * 56)
    print()

    # ─── 1. Config check ────────────────────────────
    print("🔍 Checking configuration...")
    cfg_path = os.path.expanduser("~/.config/toolrecall/toolrecall.toml")
    found = []
    errors = []

    if os.path.exists(cfg_path):
        found.append(f"config: {cfg_path}")
        from toolrecall.config import load_config

        cfg = load_config()
        allowed = cfg.mcp_allowed_paths
        if allowed:
            found.append(f"allowed_paths ({len(allowed)} dirs): {', '.join(allowed)}")
            for p in allowed:
                expanded = os.path.expanduser(p) if "~" in p else p
                if not os.path.isdir(expanded):
                    errors.append(f"allowed_path '{p}' → {expanded} does not exist")
        else:
            errors.append("allowed_paths is empty — all file reads blocked!")

        if cfg.mcp_allow_terminal:
            found.append("terminal: ENABLED")
        else:
            found.append("terminal: disabled (default)")

        if cfg.mcp_multiplex_enabled:
            servers = cfg.mcp_multiplex_servers
            found.append(f"MCP multiplexer: enabled ({len(servers)} servers)")
        else:
            found.append("MCP multiplexer: disabled")
    else:
        errors.append(
            f"config not found at {cfg_path} — run 'toolrecall setup' or 'toolrecall init'"
        )

    for msg in found:
        print(f"  ✅ {msg}")
    if errors:
        print()
        for msg in errors:
            print(f"  ❌ {msg}")
        print()

    # ─── 2. systemd restart ─────────────────────────
    print("🔄 Restarting via systemd --user...")
    result = subprocess.run(
        ["systemctl", "--user", "restart", "toolrecall-daemon"],
        capture_output=True,
        text=True,
        timeout=30,
    )

    if result.returncode != 0:
        print(f"  ⚠️  systemctl restart returned exit {result.returncode}")
        print("     (exit -15 = SIGTERM = daemon was killed; exit 3 = not running)")
        if result.stderr.strip() and result.returncode not in (-15, 3):
            for line in result.stderr.strip().split("\n"):
                print(f"     {line}")
        print("  → Falling back to direct daemon start...")
        print()

        # Fallback: start daemon directly
        from toolrecall.daemon import stop_daemon

        stop_daemon()
        if _ensure_daemon():
            print("  ✅ Daemon started via fallback")
        else:
            print("  ❌ Could not start daemon — try 'toolrecall daemon --foreground'")
            print()
            print("=" * 56)
            return

    print("  ✅ systemd restart issued successfully")

    # ─── 3. Wait for readiness ─────────────────────
    from toolrecall.transport import TransportClient, DEFAULT_PATH

    print("  Waiting for daemon to accept connections...")
    for attempt in range(10):
        time.sleep(0.5)
        try:
            tc = TransportClient(DEFAULT_PATH)
            resp = tc.send({"cmd": "ping"})
            if resp.get("pong"):
                print(
                    f"  ✅ Daemon ready (PID {resp.get('pid')}) — connected (attempt {attempt + 1})"
                )
                break
        except Exception:
            continue
    else:
        print(
            "  ⚠️  Daemon started but not responding after 5s — check 'toolrecall daemon --status'"
        )

    # ─── 4. Summary ─────────────────────────────
    print()
    print("=" * 56)
    if errors:
        print(f"  ⚠️  Restarted with {len(errors)} config issue(s) to fix")
        for msg in errors:
            print(f"     ❌ {msg}")
        print()
        print("  Fix config issues above, then run 'toolrecall restart' again.")
    else:
        print("  ✅ Restart complete — everything looks good")
    print("=" * 56)


def cmd_healthcheck():
    """Run the generalized daemon/cache healthcheck.

    Local-only: reuses ToolRecall's own transport/pid/lock paths. Exit code:
      0  healthy
      1  daemon down or abnormal pid/lock/socket state
      2  hard failure (e.g. healthcheck module import error)
    """
    try:
        from toolrecall import healthcheck as _hc

        code = _hc.run("--json" in sys.argv[2:])
    except Exception as e:  # noqa: BLE001  (surface a hard failure as exit 2)
        print(f"toolrecall healthcheck error: {e}")
        code = 2
    sys.exit(code)


def cmd_context():
    """Context tracker inspection: `toolrecall context <status|stale>`.

    Local-only: talks to the daemon over the UDS. No network, no deps.
    """
    import json as _json
    from toolrecall import client

    args = sys.argv[2:]
    sub = args[0] if args else ""

    if sub in ("", "--help", "-h", "help"):
        print("Usage: toolrecall context <command>")
        print("")
        print("Commands:")
        print("  status              Checkpoint, dirty/clean/stale counts")
        print("  stale               Files read then overwritten (stale in context)")
        print("")
        print("Options for 'stale':")
        print("  --format json|table   Output format (default: table)")
        print("  --quiet, -q           Bare paths, one per line (pipeable)")
        print("")
        print("Exit codes: 0 = nothing stale, 1 = stale files found, 2 = daemon error")
        return

    def _call(fn):
        try:
            resp = fn()
        except Exception as e:
            print(f"  \u26a0\ufe0f  Could not reach daemon: {e}", file=sys.stderr)
            sys.exit(2)
        if isinstance(resp, dict) and resp.get("error"):
            print(f"  \u26a0\ufe0f  {resp['error']}", file=sys.stderr)
            sys.exit(2)
        return resp

    if sub == "status":
        stats = _call(client.context_get_stats)
        stale = _call(client.context_get_stale)
        print("=" * 50)
        print("  Context Tracker")
        print("=" * 50)
        print(f"  checkpoint:          {stats.get('checkpoint', 0)}")
        print(f"  files read:          {stats.get('total_read', 0)}")
        print(f"  dirty (written):     {stats.get('total_dirty', 0)}")
        print(f"  clean (droppable):   {stats.get('total_clean', 0)}")
        print(f"  stale (WRONG):       {stale.get('total_stale', 0)}")
        print(f"  reclaimable:         {stale.get('est_reclaimable_tokens', 0):,} tokens")
        print(f"  dropped (cumulative):{stats.get('ctx_dropped_tokens_total', 0):,} tokens")
        return

    if sub == "stale":
        fmt = "table"
        if "--format" in args:
            i = args.index("--format")
            if i + 1 < len(args):
                fmt = args[i + 1]
        if "--quiet" in args or "-q" in args:
            fmt = "quiet"

        data = _call(client.context_get_stale)
        stale = data.get("stale", [])

        if fmt == "json":
            print(_json.dumps(data, indent=2))
        elif fmt == "quiet":
            for e in stale:
                print(e["path"])
        elif not stale:
            print("  \u2705 No stale files \u2014 every file you read is current on disk.")
        else:
            total = data.get("est_reclaimable_tokens", 0)
            print(f"  {len(stale)} stale file(s), ~{total:,} reclaimable tokens:")
            print("")
            for n, e in enumerate(stale, 1):
                print(f"  {n:>3}. {e['path']}")
                print(
                    f"       read at op {e['read_seq']}, overwritten at op "
                    f"{e['write_seq']} \u2014 ~{e['est_tokens']:,} tok"
                )
            print("")
            print("  These copies in your context are out of date. Evict or re-read.")

        sys.exit(1 if stale else 0)

    if sub == "recall":
        rsub = args[1] if len(args) > 1 else ""
        if rsub in ("", "--help", "-h", "help"):
            print("Usage: toolrecall context recall <command>")
            print("")
            print("Commands:")
            print("  store <fingerprint> [content_type]   Persist stdin content, print node_id")
            print("  get   <node_id>                      Restore a stored block")
            print(
                "  status                               Count + tokens in recall cache (if enabled)"
            )
            print("")
            print("Options for 'store':")
            print("  --reproducible    Mark block as deterministically re-fetchable")
            print('  --summary "..."    Optional short semantic pointer')
            print("Options for 'get':")
            print("  --json            Print the full entry as JSON")
            return

        if rsub == "store":
            if len(args) < 3:
                print(
                    "Usage: toolrecall context recall store <fingerprint> [content_type]",
                    file=sys.stderr,
                )
                sys.exit(2)
            fingerprint = args[2]
            content_type = args[3] if len(args) > 3 else "other"
            reproducible = "--reproducible" in args
            summary = ""
            if "--summary" in args:
                i = args.index("--summary")
                if i + 1 < len(args):
                    summary = args[i + 1]
            content = sys.stdin.read()
            if not content:
                print(
                    "  \u26a0\ufe0f  No input on stdin — pipe the content to persist.",
                    file=sys.stderr,
                )
                sys.exit(2)
            resp = _call(
                lambda: client.recall_store(
                    fingerprint=fingerprint,
                    content=content,
                    content_type=content_type,
                    reproducible=reproducible,
                    summary=summary,
                )
            )
            print(resp.get("node_id", ""))
            return

        if rsub == "get":
            if len(args) < 3:
                print("Usage: toolrecall context recall get <node_id>", file=sys.stderr)
                sys.exit(2)
            node_id_ = args[2]
            resp = _call(lambda: client.recall_get(node_id_))
            entry = resp.get("entry")
            if entry is None:
                print(f"  \u274c  No entry for node_id: {node_id_}", file=sys.stderr)
                sys.exit(1)
            if "--json" in args:
                print(_json.dumps(entry, indent=2))
                return
            if entry.get("summary"):
                print(f"# {entry['summary']}")
            print(entry.get("content", ""))
            return

        if rsub == "status":
            resp = _call(client.recall_stats)
            print(f"  recall entries:   {resp.get('total', 0)}")
            print(f"  persisted tokens: {resp.get('tokens', 0):,}")
            return

        print(f"Unknown recall command: {rsub}")
        print("Available: store, get, status")
        sys.exit(2)

    print(f"Unknown context command: {sub}")
    print("Available: status, stale, recall")
    sys.exit(2)


def main():
    if len(sys.argv) < 2 or sys.argv[1] in ("--help", "-h"):
        print("Usage: toolrecall <command>")
        print()
        print("Commands:")
        print("  setup           One-shot setup: config + systemd service + shim + start")
        print("  restart         Health check + clean daemon restart")
        print("  stop            Stop daemon + revert forward-proxy base-URL wiring")
        print("  init            Create default config.toml and .env")
        print("  status          Cache status and stats")
        print("  stats           Detailed stats (JSON)")
        print("  invalidate      Clear all caches")
        print("  reset-stats     Reset statistics counters (preserves cache entries)")
        print("  index           Build/update knowledge database")
        print("  index-memory    Index agent memory stores (MEMORY.md, USER.md)")
        print("  index-dir       Index a directory into knowledge DB (e.g. Obsidian vault)")
        print("  config-set      Set a config value (section.key = value)")
        print("  serve           Start forward proxy (cache API responses)")
        print("  debug           Start debug/demo server (test cached_read via curl)")
        print("  nginx           Generate nginx config")
        print("  mcp             Start MCP Bridge (requires daemon)")
        print("  daemon          Start/stop/manage cache daemon")
        print("  shim            Install/uninstall transparent cache shim (.pth)")
        print("  replay          Record/replay tool call scenarios (Replay mode)")
        print("  turso           Turso Cloud sync: init, status")
        return

    if sys.argv[1] in ("--version", "-V", "-v"):
        from toolrecall import __version__

        print(f"ToolRecall {__version__}")
        return

    cmd = sys.argv[1]

    # Commands that need the daemon running
    _DAEMON_REQUIRED = {
        "status",
        "stats",
        "invalidate",
        "reset-stats",
        "serve",
        "debug",
        "mcp",
        "restart",
        "index",
        "index-memory",
        "index-dir",
        "context",
    }
    if cmd in _DAEMON_REQUIRED:
        if not _ensure_daemon():
            print(f"  ⚠️  Could not start daemon — running '{cmd}' in direct mode.", file=sys.stderr)

    commands = {
        "init": cmd_init,
        "setup": cmd_setup,
        "restart": cmd_restart,
        "stop": cmd_stop,
        "status": cmd_status,
        "stats": cmd_stats,
        "invalidate": cmd_invalidate,
        "reset-stats": cmd_reset_stats,
        "index": cmd_index,
        "index-memory": cmd_index_memory,
        "index-dir": cmd_index_dir,
        "config-set": cmd_config_set,
        "serve": cmd_serve,
        "debug": cmd_debug,
        "nginx": cmd_nginx,
        "mcp": cmd_mcp,
        "daemon": cmd_daemon,
        "shim": cmd_shim,
        "healthcheck": cmd_healthcheck,
        "context": cmd_context,
    }

    if cmd in commands:
        commands[cmd]()
    elif cmd == "replay":
        from toolrecall.replay_cli import cmd_replay

        cmd_replay(sys.argv[2:])
    elif cmd == "turso":
        from toolrecall.turso_cli import cmd_turso

        cmd_turso(sys.argv[2:])
    else:
        print(f"Unknown command: {cmd}")
        print("Available: status, stats, invalidate, index, serve, nginx, mcp, daemon")
