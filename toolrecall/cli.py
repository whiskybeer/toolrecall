"""ToolRecall CLI -- toolrecall status, stats, invalidate, index, serve, debug, nginx, mcp, daemon.

Usage:
    toolrecall status          # Show cache status
    toolrecall stats           # Detailed statistics (JSON)
    toolrecall invalidate      # Clear cache
    toolrecall reset-stats     # Reset statistics counters
    toolrecall index           # Index knowledge base
    toolrecall serve           # Start forward proxy (cache API responses)
    toolrecall debug           # Start debug/demo server (test cached_read via curl)
    toolrecall nginx           # Generate nginx config
    toolrecall mcp             # Start MCP Bridge (stdio → Daemon)
    toolrecall mcp-legacy      # Start standalone MCP Server (no Daemon needed)
    toolrecall daemon          # Start Cache Daemon (background)
    toolrecall daemon --stop   # Stop Daemon
    toolrecall daemon --status # Show Daemon status
    toolrecall daemon --foreground  # Start in foreground
    toolrecall init            # Create default config.toml and .env
"""
import os, sys, json

def cmd_init():
    """Create boilerplate config and .env for users with interactive setup."""
    import os, sys
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
            prompt = "Path 1: " if first else f"Path {len(paths)+1}: "
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
"""

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
            saved = v.get("tokens_saved", 0)
            saved_str = f", tokens_saved={saved:,}" if saved else ""
            print(f"  {k}: {v['hits']} hits, {v['misses']} misses, " +
                  f"hit_rate={v['hit_rate']}, tokens_read_from_disk={v['tokens_read_from_disk']:,}{saved_str}")
        else:
            print(f"  {k}: {v}")
    # Recent activity
    recent = stats.get("recent", [])
    if recent:
        print()
        print("  ── Last 20 accesses ──")
        print(f"  {'ago':>8} {'type':<12} {'tokens':>8} {'path'}")
        print(f"  {'─'*8} {'─'*12} {'─'*8} {'─'*40}")
        for r in recent:
            icon = "✅" if r["hit"] else "⬇️"
            p = r.get("path", r["category"])
            if len(p) > 40:
                p = "..." + p[-37:]
            tokens_str = f"{r['tokens']:,}" if r.get("tokens", 0) else "-"
            print(f"  {r['ago']:>8} {icon} {tokens_str:>8} {p}")

def cmd_stats():
    """Detailed statistics as JSON."""
    try:
        from toolrecall.client import cache_status
        import re
        print(cache_status())  # String output for CLI
    except Exception:
        from toolrecall.cache import get_stats
        print(json.dumps(get_stats(), indent=2))

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
    """Index a directory into the knowledge database.
    
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
        print("  toolrecall config-set security.tool_access_control true  # MCP keyword access control (not OS sandbox)")
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
    """Start the forward proxy (caches LLM API responses)."""
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

    from toolrecall.proxy import run_forward_proxy
    port = port_override if port_override is not None else int(os.environ.get("TOOLRECALL_FORWARD_PORT", "8569"))
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
            capture_output=True, text=True, timeout=15,
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
        _toolrecall_bin = _shutil.which("toolrecall")
        if not _toolrecall_bin:
            # Fallback: locate the installed package's cli module directly
            _toolrecall_bin = _sys.executable
            _sp.Popen(
                [_toolrecall_bin, "-c", "from toolrecall.cli import cmd_daemon; import sys; sys.argv = ['toolrecall', 'daemon', '--foreground']; cmd_daemon()"],
                stdout=_sp.DEVNULL, stderr=_sp.DEVNULL,
                start_new_session=True,
            )
        else:
            _sp.Popen(
                [_toolrecall_bin, "daemon", "--foreground"],
                stdout=_sp.DEVNULL, stderr=_sp.DEVNULL,
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
                stdout=_sp.DEVNULL, stderr=_sp.DEVNULL,
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
    """Install OS-level shim if not present, then load it into the current process."""
    import os, shutil, sys
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
            # Install the .pth file
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
    import os, subprocess
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

    if "--stop" in sys.argv:
        stop_daemon()
    elif "--status" in sys.argv:
        daemon_status()
    elif "--foreground" in sys.argv:
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
    """Install or uninstall the transparent OS-level cache shim (.pth file)."""
    action = sys.argv[2] if len(sys.argv) > 2 else "status"
    
    if action == "--install" or action == "install":
        import shutil
        site_pkgs = None
        for p in sys.path:
            if p.endswith("site-packages") and os.path.isdir(p):
                site_pkgs = p
                break
        if not site_pkgs:
            print("Error: could not find site-packages directory")
            return
        
        pth_src = os.path.join(os.path.dirname(__file__), "tr_shim.pth")
        pth_dst = os.path.join(site_pkgs, "tr_shim.pth")
        shutil.copy2(pth_src, pth_dst)
        print(f"✅ Shim installed: {pth_dst}")
        print("   Every Python process will now auto-cache open() and subprocess.run()")
        print("   via the ToolRecall daemon.")
        print("   Disable with: TOOLRECALL_SHIM_DISABLE=1")
        
    elif action == "--uninstall" or action == "uninstall":
        site_pkgs = None
        for p in sys.path:
            if p.endswith("site-packages") and os.path.isdir(p):
                site_pkgs = p
                break
        if not site_pkgs:
            print("Error: could not find site-packages directory")
            return
        
        pth_path = os.path.join(site_pkgs, "tr_shim.pth")
        if os.path.exists(pth_path):
            os.remove(pth_path)
            print(f"✅ Shim removed: {pth_path}")
        else:
            print("Shim not installed.")
            
    elif action == "--status" or action == "status":
        for p in sys.path:
            if p.endswith("site-packages"):
                pth = os.path.join(p, "tr_shim.pth")
                if os.path.exists(pth):
                    print(f"✅ Shim installed: {pth}")
                    return
        print("❌ Shim not installed")
        
    else:
        print("Usage: toolrecall shim [--install|--uninstall|--status]")
        print()
        print("  --install     Install .pth file -> every Python process auto-caches")
        print("  --uninstall   Remove .pth file")
        print("  --status      Check if shim is installed")


SYSTEMD_SERVICE_CONTENT = """[Unit]
Description=ToolRecall Cache Daemon
After=network.target

[Service]
Type=simple
ExecStart=%s
Restart=on-failure
RestartSec=5s

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

        import sys
        toolrecall_bin = os.path.expanduser("~/.local/bin/toolrecall")
        if not os.path.exists(toolrecall_bin):
            import shutil
            toolrecall_bin = shutil.which("toolrecall") or toolrecall_bin
        service_content = SYSTEMD_SERVICE_CONTENT % (toolrecall_bin + " daemon --foreground")
        with open(service_path, "w") as f:
            f.write(service_content)

        subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True, timeout=10)
        subprocess.run(["systemctl", "--user", "enable", "toolrecall-daemon"], capture_output=True, timeout=10)
        steps_ok.append("systemd service: written + enabled")
    except FileNotFoundError:
        pass  # No systemd — _ensure_daemon will use fork

    # ─── 3. Shim + Daemon (auto-start) ────────────
    _ensure_shim()
    steps_ok.append("shim: installed")
    if _ensure_daemon():
        steps_ok.append("daemon: running")
    else:
        errors.append("daemon could not be started")

    # ─── 4. Agent integration (opencode) ─────────
    _ensure_agent_integration()

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
    print("=" * 56)


def _ensure_agent_integration():
    """Auto-detect opencode and write MCP config.

    Detects opencode by checking for ~/.opencode/ directory and writes
    the MCP integration config so 'toolrecall mcp' is available as an
    MCP server in opencode sessions.

    Returns True if config was written, False if no opencode found.
    """
    OC_DIR = os.path.expanduser("~/.opencode")
    if not os.path.isdir(OC_DIR):
        return False

    oc_config_path = os.path.join(OC_DIR, "opencode.jsonc")
    config = _prepare_opencode_config(oc_config_path)

    import json
    with open(oc_config_path, "w") as f:
        json.dump(config, f, indent=2)
    print(f"  ✅ opencode MCP config written: {oc_config_path}")
    return True


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
        errors.append(f"config not found at {cfg_path} — run 'toolrecall setup' or 'toolrecall init'")

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
        capture_output=True, text=True, timeout=30
    )

    if result.returncode != 0:
        print(f"  ⚠️  systemctl restart returned exit {result.returncode}")
        print(f"     (exit -15 = SIGTERM = daemon was killed; exit 3 = not running)")
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
                print(f"  ✅ Daemon ready (PID {resp.get('pid')}) — connected (attempt {attempt + 1})")
                break
        except Exception:
            continue
    else:
        print("  ⚠️  Daemon started but not responding after 5s — check 'toolrecall daemon --status'")

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


def main():
    if len(sys.argv) < 2 or sys.argv[1] in ("--help", "-h"):
        print("Usage: toolrecall <command>")
        print()
        print("Commands:")
        print("  setup           One-shot setup: config + systemd service + shim + start")
        print("  restart         Health check + clean daemon restart")
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
        return

    if sys.argv[1] in ("--version", "-V", "-v"):
        from toolrecall import __version__
        print(f"ToolRecall {__version__}")
        return

    cmd = sys.argv[1]

    # Commands that need the daemon running
    _DAEMON_REQUIRED = {
        "status", "stats", "invalidate", "reset-stats",
        "serve", "debug", "mcp", "restart", "index",
        "index-memory", "index-dir",
    }
    if cmd in _DAEMON_REQUIRED:
        # Auto-install shim if missing, then ensure daemon is running
        _ensure_shim()
        if not _ensure_daemon():
            print(f"  ⚠️  Could not start daemon — running '{cmd}' in direct mode.", file=sys.stderr)

    commands = {
        "init": cmd_init,
        "setup": cmd_setup,
        "restart": cmd_restart,
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
    }

    if cmd in commands:
        commands[cmd]()
    else:
        print(f"Unknown command: {cmd}")
        print("Available: status, stats, invalidate, index, serve, nginx, mcp, daemon")
