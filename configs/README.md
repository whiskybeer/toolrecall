# ToolRecall Agent Configs
#
# READY-TO-USE MCP-Konfigurationen für verschiedene Agenten.
# Kopieren ans Zielverzeichnis (siehe unten).
#
# Voraussetzung: `toolrecall` muss im PATH sein
# (Symlink via: sudo ln -sf ~/.local/bin/toolrecall /usr/local/bin/)
#
# ── Verzeichnisse ──────────────────────────────────────────
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
#   Inhalt in "mcpServers" einbetten
#
# Cursor:
#   ~/.cursor/mcp.json  →  cursor-mcp.json
#
# Hermes:
#   ~/.hermes/config.yaml
#   mcp_servers Abschnitt (bereits konfiguriert)
#
# Aider:
#   --mcp-toolrecall oder ~/.aider.mcp.json
#
# ── Test ───────────────────────────────────────────────────
#
#   echo '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' | toolrecall mcp
#   → Liste aller verfügbaren Cache-Tools
#
#   echo '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"cached_read","arguments":{"path":"/etc/os-release"}}}' | toolrecall mcp
#   → Gecachter Dateiinhalt
