"""ToolRecall CLI -- toolrecall status, stats, invalidate, index, serve, nginx, mcp, daemon.

Usage:
    toolrecall status          # Show cache status
    toolrecall stats           # Detailed statistics (JSON)
    toolrecall invalidate      # Clear cache
    toolrecall index           # Index knowledge base
    toolrecall serve           # Start HTTP proxy (via Daemon)
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
    """Create boilerplate config and .env for users."""
    import os
    cfg_dir = os.path.expanduser("~/.toolrecall")
    os.makedirs(cfg_dir, exist_ok=True)
    
    cfg_path = os.path.join(cfg_dir, "config.toml")
    env_path = os.path.join(cfg_dir, ".env")
    
    cfg_content = """# ToolRecall Configuration
# Created by `toolrecall init`

[storage]
backend = "sqlite"

[mcp]
# Security: Only these directories can be read by the LLM via ToolRecall File-MCP.
# Adjust this to match your project paths to prevent prompt injections!
allowed_paths = [
    "~/projects",
    "~/.hermes/skills"
]
allow_terminal = false # Set to true ONLY if you understand the security risks
allow_invalidate = false
default_ttl = 60 # Default cache time for MCP tools in seconds

[mcp_multiplex]
enabled = true
idle_minutes = 15 # Shut down MCP servers after 15 minutes of inactivity

[mcp_multiplex.servers_config]
# ToolRecall multiplexes all your MCP servers through a single connection.
# Servers are lazy-loaded on the first call and killed after 15min idle to save RAM.

# --- Useful Default Examples ---

# 1. GitHub (Manage PRs, Issues, and Repositories)
github = { command = "npx", args = ["-y", "@modelcontextprotocol/server-github"], ttl = 60 }

# 2. Sequential Thinking (Anthropic's official Chain-of-Thought Whiteboard)
# sequential-thinking = { command = "npx", args = ["-y", "@modelcontextprotocol/server-sequential-thinking"] }

# 3. Brave Search (Live web search for AI agents to look up fresh documentation)
# brave-search = { command = "npx", args = ["-y", "@modelcontextprotocol/server-brave-search"], ttl = 3600 }

# 4. Fetch (Convert any URL into clean Markdown, aggressively cached by ToolRecall)
# fetch = { command = "uvx", args = ["mcp-server-fetch"], ttl = 3600 }

# 5. PostgreSQL (Allow your agent to query a local or remote database)
# postgres = { command = "npx", args = ["-y", "@modelcontextprotocol/server-postgres", "postgresql://localhost/mydb"], ttl = 30 }

# Note: For servers that POST data or stream live updates (like Slack), set `ttl = 0` to bypass caching!
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
    
    created_env = False
    if not os.path.exists(env_path):
        with open(env_path, "w") as f:
            f.write(env_content)
        # Protect .env
        os.chmod(env_path, 0o600)
        created_env = True
        
    print(f"ToolRecall directory: {cfg_dir}")
    if created_cfg:
        print("✅ Created default config.toml")
    else:
        print("ℹ️ config.toml already exists")
        
    if created_env:
        print("✅ Created default .env")
    else:
        print("ℹ️ .env already exists")
        
    print("\nNext steps:")
    print("1. Edit ~/.toolrecall/config.toml to add your project paths.")
    print("2. Edit ~/.toolrecall/.env to add your API keys.")
    print("3. Start the daemon: toolrecall daemon &")

def cmd_status():
    """Show cache status via daemon or directly."""
    try:
        from toolrecall.client import cache_status
        print(cache_status())
    except Exception:
        from toolrecall.cache import get_stats
        stats = get_stats()
        print("=" * 50)
        print("  ToolRecall Status (direct)")
        print("=" * 50)
        for k, v in stats.items():
            if isinstance(v, dict):
                print(f"  {k}: {v['hits']} hits, {v['misses']} misses, " +
                      f"hit_rate={v['hit_rate']}, tokens_saved={v['tokens_saved']:,}")
            else:
                print(f"  {k}: {v}")

def cmd_stats():
    """Detailed statistics as JSON."""
    try:
        from toolrecall.client import cache_status
        import re
        print(cache_status())  # String output for CLI
    except Exception:
        from toolrecall.cache import get_stats
        print(json.dumps(get_stats(), indent=2))

def cmd_gc():
    """Run Garbage Collection to free SQLite space."""
    from toolrecall.cache import garbage_collect
    print("Running Garbage Collection...")
    cleaned = garbage_collect()
    if cleaned >= 0:
        print(f"✅ GC complete. Removed {cleaned} expired items and vacuumed SQLite WAL.")
    else:
        print("❌ GC failed. Check daemon logs.")

def cmd_invalidate():
    """Clear cache via Daemon or direct SQLite fallback."""
    try:
        from toolrecall.client import cache_invalidate
        print(cache_invalidate())
    except Exception:
        from toolrecall.cache import invalidate_all
        invalidate_all()
        print("ToolRecall cache cleared (direct).")

def cmd_index():
    """Index knowledge base. Use --memory to also index Hermes memory stores."""
    from toolrecall.docs import index_all, index_hermes_memory
    print("Indexing knowledge database...")
    total = index_all()
    print(f"Done. {total} pages indexed.")
    
    if "--memory" in sys.argv:
        print("Indexing Hermes memory stores...")
        mem_total = index_hermes_memory()
        print(f"Done. {mem_total} memory entries indexed.")

def cmd_index_memory():
    """Index Hermes persistent memory stores (MEMORY.md, USER.md) into knowledge DB."""
    from toolrecall.docs import index_hermes_memory

    # Optional: custom source label via --source
    source = "hermes-memory"
    if "--source" in sys.argv:
        idx = sys.argv.index("--source")
        if idx + 1 < len(sys.argv):
            source = sys.argv[idx + 1]

    print(f"Indexing Hermes memory stores (source='{source}')...")
    total = index_hermes_memory(source=source)
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
    from toolrecall.config import load_config, save_config, _have_tomli_w

    if not _have_tomli_w():
        print("❌ tomli-w not installed. Run: pip install toolrecall[toml-write]")
        return

    args = sys.argv[2:]
    if len(args) < 2 or "--help" in args or "-h" in args:
        print("Usage: toolrecall config-set <section.key> <value>")
        print()
        print("Examples:")
        print("  toolrecall config-set proxy.port 9090")
        print("  toolrecall config-set mcp.allow_terminal true")
        print("  toolrecall config-set security.read_only_sandbox true")
        print("  toolrecall config-set mcp.allowed_paths \"['/data', '/projects']\"")
        return

    key = args[0]
    val = args[1]
    parts = key.rsplit(".", 1)

    if len(parts) != 2:
        print(f"❌ Invalid key: '{key}'. Use section.key format (e.g. proxy.port)")
        return

    section, name = parts
    cfg_path = os.path.expanduser("~/.toolrecall/config.toml")
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
    """Start HTTP proxy (via Daemon)."""
    if "--help" in sys.argv or "-h" in sys.argv:
        print("Usage: toolrecall serve")
        print()
        print("Start the ToolRecall HTTP proxy server.")
        print()
        print("Options:")
        print("  --help, -h    Show this help message")
        print()
        print("Configuration:")
        print("  Port:    proxy.port in config.toml (default: 8567)")
        print("  Bind:    proxy.bind in config.toml (default: 0.0.0.0)")
        print()
        print("Endpoints:")
        print("  GET /cached_read?path=...")
        print("  GET /cached_skill?name=...")
        print("  GET /cached_terminal?cmd=...&ttl=...")
        print("  GET /docs_search?query=...")
        print("  GET /health")
        print()
        print("Recommended: put nginx in front for SSL + auth.")
        print("  toolrecall nginx  ->  generates nginx config")
        return
    from toolrecall.proxy import run_server
    from toolrecall.config import load_config
    cfg = load_config()
    run_server(bind=cfg.proxy_bind, port=cfg.proxy_port)

def cmd_mcp():
    """Start MCP Bridge (stdio → Daemon)."""
    from toolrecall.mcp_bridge import main as bridge_main
    bridge_main()

def cmd_mcp_legacy():
    """Start standalone MCP Server (no Daemon needed, legacy)."""
    from toolrecall.mcp_server import main as legacy_main
    legacy_main()

def cmd_daemon():
    """Manage the ToolRecall Cache Daemon."""
    from toolrecall.daemon import run_daemon, stop_daemon, daemon_status

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
        proxy_pass http://127.0.0.1:8567/;
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
    #     proxy_pass http://127.0.0.1:8567/;
    # }
}

# SSL Version (recommended)
# server {
#     listen 443 ssl;
#     server_name toolrecall.dev;
#     ssl_certificate /etc/letsencrypt/live/toolrecall.dev/fullchain.pem;
#     ssl_certificate_key /etc/letsencrypt/live/toolrecall.dev/privkey.pem;
#     location /toolrecall/ {
#         proxy_pass http://127.0.0.1:8567/;
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

def cmd_export_dataset():
    """Export local cache to a JSONL dataset for AI Fine-Tuning."""
    import sys
    out_path = "toolrecall_dataset.jsonl"
    if len(sys.argv) > 2:
        out_path = sys.argv[2]
        
    try:
        from toolrecall.dataset import export_trajectories
        count, path = export_trajectories(out_path)
        print(f"✅ Exported {count} tool trajectories to {path}")
        print("Format: JSONL (Ready for HuggingFace / Supervised Fine-Tuning)")
    except Exception as e:
        print(f"Error exporting dataset: {e}")

def main():
    if len(sys.argv) < 2:
        print("Usage: toolrecall <command>")
        print()
        print("Commands:")
        print("  init            Create default config.toml and .env")
        print("  status          Cache status and stats")
        print("  stats           Detailed stats (JSON)")
        print("  invalidate      Clear all caches")
        print("  index           Build/update knowledge database")
        print("  index-memory    Index Hermes memory stores (MEMORY.md, USER.md)")
        print("  index-dir       Index a directory into knowledge DB (e.g. Obsidian vault)")
        print("  config-set      Set a config value (section.key = value)")
        print("  export-dataset")
        print("  serve           Start HTTP proxy")
        print("  nginx           Generate nginx config")
        print("  mcp             Start MCP Bridge (requires daemon)")
        print("  mcp-legacy      Start standalone MCP Server (no daemon)")
        print("  daemon          Start/stop/manage cache daemon")
        return

    cmd = sys.argv[1]
    commands = {
        "init": cmd_init,
        "status": cmd_status,
        "stats": cmd_stats,
        "invalidate": cmd_invalidate,
        "index": cmd_index,
        "index-memory": cmd_index_memory,
        "index-dir": cmd_index_dir,
        "config-set": cmd_config_set,
        "export-dataset": cmd_export_dataset,
        "serve": cmd_serve,
        "nginx": cmd_nginx,
        "mcp": cmd_mcp,
        "mcp-legacy": cmd_mcp_legacy,
        "daemon": cmd_daemon,
    }

    if cmd in commands:
        commands[cmd]()
    else:
        print(f"Unknown command: {cmd}")
        print("Available: status, stats, invalidate, index, serve, nginx, mcp, mcp-legacy, daemon")
