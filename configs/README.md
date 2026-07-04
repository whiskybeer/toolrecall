# ToolRecall Agent Configs
#
# READY-TO-USE MCP configurations for various agents.
# Copy to the target directory (see below).
#
# Prerequisite: `toolrecall` must be on PATH
# (symlink via: sudo ln -sf ~/.local/bin/toolrecall /usr/local/bin/)
#
# ── Directories ──────────────────────────────────────────
#
# opencode (Node.js v1.17+):
#   ~/.opencode/opencode.jsonc
#   Format (Crush):
#     "mcp": {
#       "SERVERNAME": {
#         "type": "local",
#         "command": "toolrecall",
#         "args": ["mcp"],
#         "enabled": true
#       }
#     }
#
# Claude Code:
#   ~/.claude/settings.json  →  claude-code.json
#   Embed inside "mcpServers" section
#
# Cursor:
#   ~/.cursor/mcp.json  →  cursor-mcp.json
#
# Hermes:
#   ~/.hermes/config.yaml
#   mcp_servers section (already configured)
#
# Aider:
#   --mcp-toolrecall or ~/.aider.mcp.json
#
# ── Test ───────────────────────────────────────────────────
#
#   echo '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' | toolrecall mcp
#   → Lists all available cache tools
#
#   echo '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"cached_read","arguments":{"path":"/etc/os-release"}}}' | toolrecall mcp
#   → Cached file content
#