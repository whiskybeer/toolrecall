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
    print("3. Start the daemon: toolrecall daemon")

def cmd_status():
    """Show cache status via Daemon oder direkt."""
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
    """Index knowledge base."""
    from toolrecall.docs import index_all
    print("Indexing knowledge database...")
    total = index_all()
    print(f"Done. {total} pages indexed.")

def cmd_serve():
    """Start HTTP proxy (via Daemon)."""
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
        proxy_pass http://[IP_ADDRESS]:8567/;
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
    #     proxy_pass http://[IP_ADDRESS]:8567/;
    # }
}

# SSL Version (recommended)
# server {
#     listen 443 ssl;
#     server_name toolrecall.dev;
#     ssl_certificate /etc/letsencrypt/live/toolrecall.dev/fullchain.pem;
#     ssl_certificate_key /etc/letsencrypt/live/toolrecall.dev/privkey.pem;
#     location /toolrecall/ {
#         proxy_pass http://[IP_ADDRESS]:8567/;
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
        print("  export-dataset  [PRIVATE] Export tool calls to JSONL for AI training")
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
