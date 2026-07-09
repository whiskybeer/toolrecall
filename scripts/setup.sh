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
#   --proxy PORT     HTTP proxy port (default: 8569, 0 = disable)
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
    echo "  Claude Code:                bash setup.sh --proxy 8569 --scan /projects"
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
echo ""
echo "  ToolRecall is a pure-Python package with zero dependencies —"
echo "  only Python 3.11+ and pip (standard Python package manager)."
echo ""
echo "  ────────────────────────────────────────────────"
echo "  Install methods (choose ONE):"
echo ""
echo "    🥇 Recommended — from PyPI (via pipx):"
echo "    pipx install toolrecall"
echo "    → Stable release, isolated venv, auto-entry-points"
echo ""
echo "    🥑 Using pip (alternative):"
echo "       pip install toolrecall"
echo "       → Standard release, installs into current environment"
echo ""
echo "  ────────────────────────────────────────────────"
echo ""

# Try pipx first (preferred for standalone CLI tools), fall back to pip
if command -v pipx &>/dev/null; then
    echo "  Installing via pipx (recommended for CLI tools)..."
    pipx install toolrecall 2>/dev/null || pipx install --force toolrecall 2>/dev/null || {
        echo "  ⚠ pipx install failed, falling back to pip..."
        pip install toolrecall 2>/dev/null || pip3 install toolrecall 2>/dev/null
    } || {
        echo "  ❌ Installation failed."
        echo "  Install manually: pipx install toolrecall"
        echo "  (or if you prefer pip: pip install toolrecall)"
        exit 1
    }
else
    pip install toolrecall 2>/dev/null || pip3 install toolrecall 2>/dev/null || pip install git+https://github.com/whiskybeer/toolrecall.git 2>/dev/null || {
        echo "  ❌ pip not found."
        echo "  ToolRecall requires Python 3.11+ with pip."
        echo "  Recommended install: pipx install toolrecall"
        echo "  Or install Python first: https://python.org/downloads/"
        exit 1
    }
fi
echo "  ✓ ToolRecall v$(python3 -c 'from toolrecall import __version__; print(__version__)' 2>/dev/null || echo 'installed')"
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
echo "    • Port 8569 (default) — lightweight, no dependencies"
echo "    • Port 0              — disable HTTP proxy entirely"
echo "    • Nginx in front      — for SSL + password protection"
echo ""

if [ -z "$PROXY_PORT" ]; then
    PROXY_PORT="8569"
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
[forward_proxy]
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

# --- 5. Agent integration ---
echo "[5/5] Agent integration"
echo ""

# Detect available agents
HERMES_FOUND=false
CLAUDE_FOUND=false
CURSOR_FOUND=false
CLINE_FOUND=false
OPENCODE_FOUND=false

command -v hermes &>/dev/null && HERMES_FOUND=true
command -v claude &>/dev/null && CLAUDE_FOUND=true
command -v cursor &>/dev/null && CURSOR_FOUND=true
test -f ~/.config/opencode/opencode.json && OPENCODE_FOUND=true
# Cline: check for common config locations
test -f ~/.clinerules && CLINE_FOUND=true

if [ "$HERMES_FOUND" = true ] && [ -z "$NO_RC" ]; then
    echo "  ✓ Hermes Agent detected"
    echo ""
    echo "  Hermes uses the OS-level .pth shim for transparent caching."
    echo "  The shim patches builtins.open() and subprocess.run() on every"
    echo "  Python process — no per-agent config needed."
    echo ""

    # Install the .pth shim into site-packages
    toolrecall shim --install 2>/dev/null && echo "  → .pth shim installed ✓" || {
        echo "  ⚠ Could not install .pth shim automatically."
        echo "    Run manually: toolrecall shim --install"
    }
    echo ""
    echo "  → Restart Hermes or run /reset to activate"
    echo ""
fi

# --- Non-Hermes agents: ask for config snippets ---
echo ""
echo "  ────────────────────────────────────────────"
echo "  Other AI agents"
echo "  ────────────────────────────────────────────"
echo ""
echo "  Non-Hermes agents (Claude Code, Cursor, Cline, OpenCode)"
echo "  use ToolRecall via MCP. The tools are named"
echo "  'cached_read', 'cached_terminal', etc."
echo ""
echo "  These agents default to native 'read_file', which"
echo "  BYPASSES the cache. You must tell them to use"
echo "  'cached_read' instead."
echo ""
echo "  ToolRecall can write a config snippet for each"
echo "  detected agent. No performance loss — same stdio,"
echo "  same daemon, zero network overhead."
echo ""

if [ "$CLAUDE_FOUND" = true ] && [ -z "$NO_RC" ]; then
    echo "  ✓ Claude Code detected"
    echo "  Write agent instruction to ~/.claude/claude_dotfiles/claude.md?"
    read -p "  [y/N] " ADD_CLAUDE
    if [ "$ADD_CLAUDE" = "y" ] || [ "$ADD_CLAUDE" = "Y" ]; then
        mkdir -p ~/.claude/claude_dotfiles
        cat >> ~/.claude/claude_dotfiles/claude.md << 'EOF'

## ToolRecall Cache
ToolRecall is installed. When reading files, use `cached_read` via MCP instead of `read_file`.
When running terminal commands, use `cached_terminal` instead of `terminal`.
EOF
        echo "  ✓ Written to ~/.claude/claude_dotfiles/claude.md"
    else
        echo "  → Skipped."
    fi
    echo ""
fi

if [ "$CURSOR_FOUND" = true ] && [ -z "$NO_RC" ]; then
    echo "  ✓ Cursor detected"
    # Find project root (best-effort)
    CURSOR_RULES=""
    if [ -f ".cursorrules" ]; then
        CURSOR_RULES=".cursorrules (project root)"
    elif [ -f "../.cursorrules" ]; then
        CURSOR_RULES="../.cursorrules"
    fi

    if [ -n "$CURSOR_RULES" ]; then
        echo "  Append ToolRecall instructions to $CURSOR_RULES?"
        read -p "  [y/N] " ADD_CURSOR
        if [ "$ADD_CURSOR" = "y" ] || [ "$ADD_CURSOR" = "Y" ]; then
            cat >> "$CURSOR_RULES" << 'EOF'

## ToolRecall Cache
Use cached_read for file reads (MCP tool, faster on repeats).
Use cached_terminal for terminal commands (MCP tool, TTL-cached).
EOF
            echo "  ✓ Appended to $CURSOR_RULES"
        fi
    else
        echo "  No .cursorrules found. Create one?"
        read -p "  [y/N] " ADD_CURSOR_RULES
        if [ "$ADD_CURSOR_RULES" = "y" ] || [ "$ADD_CURSOR_RULES" = "Y" ]; then
            cat > .cursorrules << 'EOF'
## ToolRecall Cache
Use cached_read for file reads (MCP tool, faster on repeats).
Use cached_terminal for terminal commands (MCP tool, TTL-cached).
EOF
            echo "  ✓ Created .cursorrules"
        fi
    fi
    echo ""
fi

if [ "$OPENCODE_FOUND" = true ] && [ -z "$NO_RC" ]; then
    echo "  ✓ OpenCode detected"
    echo "  Write agent instruction to OpenCode config?"
    read -p "  [y/N] " ADD_OPENCODE
    if [ "$ADD_OPENCODE" = "y" ] || [ "$ADD_OPENCODE" = "Y" ]; then
        OPENCODE_CONFIG="$HOME/.config/opencode/opencode.json"
        if [ -f "$OPENCODE_CONFIG" ]; then
            # Try to add system prompt instruction
            python3 -c "
import json
with open('$OPENCODE_CONFIG') as f:
    cfg = json.load(f)
instructions = cfg.get('instructions', '')
recall_note = '\n\n## ToolRecall Cache\nToolRecall is installed. When reading files, use cached_read via MCP instead of read_file. When running terminal commands, use cached_terminal instead of terminal.'
if 'ToolRecall' not in instructions:
    cfg['instructions'] = instructions + recall_note
    with open('$OPENCODE_CONFIG', 'w') as f:
        json.dump(cfg, f, indent=2)
    print('✓ Updated OpenCode config with ToolRecall instructions')
else:
    print('→ ToolRecall already in OpenCode config')
" 2>/dev/null || echo "  ⚠ Could not update OpenCode config automatically"
        else
            echo "  ⚠ Config file not found at $OPENCODE_CONFIG"
            echo "  Add manually to your OpenCode instructions:"
            echo '    "use cached_read for file reads, cached_terminal for terminal commands"'
        fi
    else
        echo "  → Skipped."
    fi
    echo ""
fi

if [ "$CLINE_FOUND" = true ] && [ -z "$NO_RC" ]; then
    echo "  ✓ Cline detected (via .clinerules)"
    echo "  Append to .clinerules?"
    read -p "  [y/N] " ADD_CLINE
    if [ "$ADD_CLINE" = "y" ] || [ "$ADD_CLINE" = "Y" ]; then
        cat >> .clinerules << 'EOF'

## ToolRecall Cache
When reading files, always use cached_read instead of read_file.
When running terminal commands, use cached_terminal.
EOF
        echo "  ✓ Appended to .clinerules"
    fi
    echo ""
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
