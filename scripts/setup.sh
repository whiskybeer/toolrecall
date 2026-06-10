#!/usr/bin/env bash
# ToolRecall Setup — one-time configuration for any LLM agent
#
# Usage:
#   git clone https://github.com/whiskybeer/toolrecall.git
#   cd toolrecall
#   bash scripts/setup.sh
#   bash scripts/setup.sh --help
#
# Options:
#   --db PATH        Cache database path (default: ~/.toolrecall/cache.db)
#   --knowledge PATH Knowledge database path (default: ~/.toolrecall/knowledge.db)
#   --proxy PORT     HTTP proxy port (default: 8567, 0 = disable)
#   --bind ADDR      HTTP proxy bind address (default: 127.0.0.1)
#   --nginx          Generate nginx config in addition
#   --scan DIRS      Comma-separated dirs to index for knowledge search
#   --no-rc          Don't add to agent init scripts
#   --help           Show this help

set -e

show_help() {
    sed -n '2,18p' "$0"
    echo ""
    echo "Examples:"
    echo "  Default (Hermes-style):    bash setup.sh"
    echo "  Claude Code:                bash setup.sh --proxy 8567 --scan /projects"
    echo "  Codex (no HTTP proxy):      bash setup.sh --proxy 0"
    echo "  Custom paths:               bash setup.sh --db /data/cache.db --proxy 0"
    exit 0
}

[ "$1" = "--help" ] && show_help

# --- Parse args ---
DB_PATH=""
KNOWLEDGE_PATH=""
PROXY_PORT=""
BIND_ADDR=""
SCAN_DIRS=""
GEN_NGINX=""
NO_RC=""
POSITIONAL=""
while [ $# -gt 0 ]; do
    case "$1" in
        --db)         DB_PATH="$2"; shift 2 ;;
        --knowledge)  KNOWLEDGE_PATH="$2"; shift 2 ;;
        --proxy)      PROXY_PORT="$2"; shift 2 ;;
        --bind)       BIND_ADDR="$2"; shift 2 ;;
        --scan)       SCAN_DIRS="$2"; shift 2 ;;
        --nginx)      GEN_NGINX="1"; shift ;;
        --no-rc)      NO_RC="1"; shift ;;
        --help)       show_help ;;
        *)            POSITIONAL="$POSITIONAL $1"; shift ;;
    esac
done

echo ""
echo "╔══════════════════════════════════════════════╗"
echo "║        ToolRecall Setup                      ║"
echo "║  Universal Tool-Output Cache for LLM Agents  ║"
echo "╚══════════════════════════════════════════════╝"
echo ""

# --- 1. Install ---
echo "[1/5] Installing ToolRecall..."
pip install toolrecall 2>/dev/null || pip3 install toolrecall 2>/dev/null || pip install git+https://github.com/Robin/toolrecall.git 2>/dev/null || {
    echo "  ⚠ pip not found. Trying uv..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    uv pip install toolrecall
}
echo "  ✓ Installed"
echo ""

# --- 2. Database location ---
echo "[2/5] Database storage"
echo ""
echo "  ToolRecall uses two SQLite databases:"
echo "    • cache.db     — cache for tool outputs (file reads, terminal, etc.)"
echo "    • knowledge.db — full-text search index (optional)"
echo ""
echo "  Recommended:"
echo "    • Default (~/.toolrecall/):   Simple, no config needed"
echo "    • Project-local (./.tr/):     Each project has its own cache"
echo "      (set via TOOLRECALL_CACHE_DB)"
echo ""

if [ -z "$DB_PATH" ]; then
    DB_PATH="~/.toolrecall/cache.db"
    echo "  → Using default: $DB_PATH"
else
    echo "  → Using: $DB_PATH"
fi
if [ -z "$KNOWLEDGE_PATH" ]; then
    KNOWLEDGE_PATH="~/.toolrecall/knowledge.db"
fi

# Create config
TR_DIR=$(eval echo "${DB_PATH%/*}")
mkdir -p "$TR_DIR"

# Write config
CONFIG_FILE="$TR_DIR/toolrecall.toml"
cat > "$CONFIG_FILE" << TOML
[paths]
cache_db = "$DB_PATH"
knowledge_db = "$KNOWLEDGE_PATH"

TOML
echo ""
echo "  Config written to: $CONFIG_FILE"
echo ""

# --- 3. HTTP Proxy ---
echo "[3/5] HTTP Proxy"
echo ""
echo "  The HTTP proxy allows agents that cannot import Python"
echo "  (Claude Code, Codex, Cursor) to use ToolRecall."
echo ""
echo "  Options:"
echo "    • Port 8567 (default) — lightweight, no dependencies"
echo "    • Port 0              — disable HTTP proxy entirely"
echo "    • Nginx in front      — for SSL + password protection"
echo ""

if [ -z "$PROXY_PORT" ]; then
    PROXY_PORT="8567"
    echo "  → Proxy enabled on port $PROXY_PORT"
fi

if [ "$PROXY_PORT" != "0" ]; then
    BIND_ADDR="${BIND_ADDR:-127.0.0.1}"

    # Test if bind address resolves (common issue on cloud VMs)
    echo -n "  Testing bind address..."
    BIND_OK="yes"
    python3 -c "import socket; socket.getaddrinfo('$BIND_ADDR', $PROXY_PORT)" 2>/dev/null || BIND_OK="no"

    if [ "$BIND_OK" = "no" ]; then
        echo " FAIL"
        echo ""
        echo "  ⚠ '$BIND_ADDR' does not resolve on this system."
        echo "    This is common on cloud VMs (GCP, AWS) with custom hostnames."
        echo "    Options:"
        echo "      127.0.0.1 (all interfaces)  → works everywhere"
        echo "      127.0.0.1 (localhost only)  → works everywhere (secure)"
        echo ""
        echo "  → Using 127.0.0.1 (all interfaces)."
        echo "    Set TOOLRECALL_PROXY_BIND=127.0.0.1 for localhost-only."
        BIND_ADDR="127.0.0.1"
    else
        echo " OK"
    fi

    cat >> "$CONFIG_FILE" << TOML
[proxy]
port = $PROXY_PORT
bind = "$BIND_ADDR"

TOML
    echo "  → Binding to $BIND_ADDR:$PROXY_PORT"
    echo "  (Change with --bind or edit $CONFIG_FILE)"

    if [ -n "$GEN_NGINX" ]; then
        echo "  → Generating nginx config..."
        toolrecall nginx 2>/dev/null || echo "  (toolrecall not yet in PATH, run 'toolrecall nginx' later)"
    fi
    echo ""
    echo "  Test your proxy:"
    echo "    toolrecall serve &"
    echo "    curl http://$BIND_ADDR:$PROXY_PORT/health"
else
    echo "  → HTTP proxy disabled (--proxy 0)"
    echo "  Agents must import ToolRecall via Python directly."
fi
echo ""

# --- 4. Knowledge scan dirs ---
echo "[4/5] Knowledge search sources"
echo ""
echo "  ToolRecall can index files for full-text search (BM25)."
echo "  Specify which directories to scan:"
echo ""

if [ -z "$SCAN_DIRS" ]; then
    # Auto-detect common project dirs
    CANDIDATES=""
    [ -d "./src" ] && CANDIDATES="$CANDIDATES, ./src"
    [ -d "./docs" ] && CANDIDATES="$CANDIDATES, ./docs"
    [ -d "./scripts" ] && CANDIDATES="$CANDIDATES, ./scripts"
    [ -d "./skills" ] && CANDIDATES="$CANDIDATES, ./skills"
    [ -d "./toolrecall" ] && CANDIDATES="$CANDIDATES, ./toolrecall"
    CANDIDATES="${CANDIDATES#, }"

    if [ -n "$CANDIDATES" ]; then
        echo "  Detected project dirs: $CANDIDATES"
        SCAN_DIRS="$CANDIDATES"
    else
        echo "  No project dirs detected. Using current directory."
        SCAN_DIRS="."
    fi
fi

cat >> "$CONFIG_FILE" << TOML
[sources]
scan_dirs = [$(echo "$SCAN_DIRS" | sed 's/, */, "/g; s/^/"/; s/$/"/; s/, "/", "/g')]
scan_extensions = [".md", ".py", ".js", ".ts", ".html", ".css", ".json", ".sh"]
scan_ignore = [".git", "node_modules", ".venv", "dist", "build", "__pycache__"]

TOML
echo "  → Scanning: $SCAN_DIRS"
echo "  (Change with --scan or edit $CONFIG_FILE)"
echo ""

# --- 5. Agent init ---
echo "[5/5] Agent integration"
echo ""

# Check what agents are available
if command -v hermes &>/dev/null && [ -z "$NO_RC" ]; then
    echo "  ✓ Hermes Agent detected"
    echo ""
    echo "  ToolRecall runs at 3 levels with Hermes. Recommend:"
    echo ""
    echo "   🥇 Level 1 — Python import (BEST)"
    echo "      → Zero network, full API, 0ms overhead"
    echo "      → Register auto-cache init script..."
    mkdir -p ~/.toolrecall
    curl -sL https://raw.githubusercontent.com/Robin/toolrecall/main/toolrecall/hermes_init.py \
        -o ~/.toolrecall/hermes_init.py 2>/dev/null || true
    hermes config set agent.init_scripts '["~/.toolrecall/hermes_init.py"]' 2>/dev/null || true
    echo "      → Restart Hermes or run /reset"
    echo ""

    echo "   🥈 Level 2 — MCP server (via mcp_servers)"
    echo "      → stdio-local, auto-injected tools"
    echo "      → Safe tools: cached_read, cached_skill, docs_search, docs_get_page, cache_status"
    echo ""
    echo "  Add to ~/.hermes/config.yaml ?"
    read -p "  [y/N] " ADD_MCP
    if [ "$ADD_MCP" = "y" ] || [ "$ADD_MCP" = "Y" ]; then
        # Check if mcp_servers section exists
        if grep -q "mcp_servers:" ~/.hermes/config.yaml 2>/dev/null; then
            # Insert before the first non-mcp_servers line after the block
            # Simple: append toolrecall entry after existing mcp_servers
            sed -i '/^mcp_servers:/a\  toolrecall:\n    command: uv\n    args:\n    - run\n    - python\n    - -m\n    - toolrecall.mcp_server\n    timeout: 30' ~/.hermes/config.yaml
        else
            cat >> ~/.hermes/config.yaml << 'YAML'

mcp_servers:
  toolrecall:
    command: uv
    args:
    - run
    - python
    - -m
    - toolrecall.mcp_server
    timeout: 30
YAML
        fi
        echo "  ✓ Added to mcp_servers. Restart Hermes to activate."
    else
        echo "  → Skipped. Add manually anytime:"
        echo "    mcp_servers:"
        echo "      toolrecall:"
        echo '        command: "uv"'
        echo '        args: ["run", "python", "-m", "toolrecall.mcp_server"]'
        echo "        timeout: 30"
    fi
    echo ""

    echo "   🥉 Level 3 — HTTP proxy (for non-Python agents)"
    if [ "$PROXY_PORT" != "0" ]; then
        echo "      → Already configured on port $PROXY_PORT"
    else
        echo "      → Not configured (--proxy 0)"
    fi
    echo ""

    echo "  ────────────────────────────────────────────"
    echo "  SECURITY: MCP server is locked down by default"
    echo "    • cached_read → restricted to ~/.hermes/skills, ~/.hermes/scripts, ~/.toolrecall"
    echo "    • cached_terminal → DISABLED (set mcp.allow_terminal=true to enable)"
    echo "    • cache_invalidate → DISABLED (set mcp.allow_invalidate=true to enable)"
    echo ""
    echo "  To customize: edit ~/.toolrecall/toolrecall.toml [mcp] section"
    echo "  ────────────────────────────────────────────"
    echo ""
fi

if command -v claude &>/dev/null; then
    echo "  ✓ Claude Code detected"
    if [ "$PROXY_PORT" != "0" ]; then
        echo "    → Use: toolrecall serve (then configure tool_server in claude.json)"
    fi
fi

if [ "$PROXY_PORT" = "0" ]; then
    echo ""
    echo "  No HTTP proxy — import ToolRecall directly:"
    echo "    from toolrecall import cached_read, cached_terminal"
fi

echo ""
echo "╔══════════════════════════════════════════════╗"
echo "║  Setup complete!                             ║"
echo "║                                              ║"
echo "║  Config:   $CONFIG_FILE"
echo "║  Database: $DB_PATH"
echo "║  Proxy:    ${PROXY_PORT}:${BIND_ADDR:-disabled}"
echo "║  MCP:      toolrecall mcp"
echo "║                                              ║"
echo "║  Start:    toolrecall serve                  ║"
echo "║            toolrecall mcp                    ║"
echo "║  Status:   toolrecall status                 ║"
echo "║  Help:     toolrecall --help                 ║"
echo "╚══════════════════════════════════════════════╝"
echo ""
echo "Next:"
echo "  • Python import: from toolrecall import cached_read"
echo "  • HTTP proxy:    toolrecall serve &"
echo "  • MCP server:    toolrecall mcp"
