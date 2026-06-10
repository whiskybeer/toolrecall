# Cursor IDE Integration

Cursor supports MCP (Model Context Protocol) natively. ToolRecall works perfectly as an in-memory cache & MCP Multiplexer for Cursor's agentic features (like "Composer").

*The term "L1 Cache" is used as a metaphor for the fastest caching tier — see the [physical limitations note](../README.md#physical-limitations-the-l1-cache-metaphor).*

## Setup

1. Open Cursor Settings (Gear Icon)
2. Go to **Features** -> **MCP** (Model Context Protocol)
3. Click **+ Add new MCP server**
4. Configure as follows:
   * **Name**: `toolrecall`
   * **Type**: `command`
   * **Command**: `toolrecall mcp`

## Why use ToolRecall with Cursor?

Cursor naturally reads many files when indexing and composing code. If you use ToolRecall as your MCP hub:
- **Zero Latency**: Re-reading large codebase files across multiple Composer turns drops to <0.1ms.
- **Cache Hit Guarantee**: Because ToolRecall serves the exact byte-identical payload from SQLite, Anthropic/Claude's Prompt Caching hits 100% of the time, saving 90% of the token costs during long Cursor sessions.
- **Security**: You can restrict Cursor's file access by configuring ToolRecall's `allow_list` in `~/.toolrecall/config.toml`.