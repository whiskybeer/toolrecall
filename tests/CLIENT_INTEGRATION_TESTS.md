# Client Integration & GUI Tests

Since ToolRecall integrates with third-party GUI clients (Claude Desktop, Cursor, MCP Inspector), these integrations cannot be fully automated via standard `pytest` CI/CD pipelines.

Instead, this document serves as the standard operating procedure (SOP) for manual integration testing before a major release.

## 1. Claude Desktop Test
**Goal**: Verify ToolRecall correctly intercepts and caches Claude Desktop file reads.
1. Copy `examples/claude-desktop/claude_desktop_config.json` to `~/Library/Application Support/Claude/claude_desktop_config.json` (Mac) or `%APPDATA%\Claude\claude_desktop_config.json` (Windows).
2. Restart Claude Desktop.
3. Open a new chat and ask: "Use the cached_read tool to read [path_to_large_file]".
4. Ask Claude to read it again.
5. **Verification**: Run `toolrecall daemon --status` in your terminal and verify the `Hits` counter has increased.

## 2. Cursor IDE Test
**Goal**: Verify Cursor's Composer works through ToolRecall.
1. In Cursor Settings -> Features -> MCP, add `toolrecall` (Command: `toolrecall mcp`).
2. Open the Cursor Composer (Cmd+I or Ctrl+I).
3. Ask Cursor to analyze a local file using the `cached_read` tool.
4. **Verification**: Check `~/.toolrecall/daemon.log` to ensure the JSON-RPC traffic from Cursor is being successfully parsed and routed.

## 3. MCP Inspector Test
**Goal**: Verify raw standard compliance using the official testing suite.
1. Run: `npx @modelcontextprotocol/inspector toolrecall mcp`
2. Open the provided localhost URL in your browser.
3. **Verification**: Ensure all native tools (`cached_read`, `cache_status`) and multiplexed tools appear in the left sidebar and can be executed without JSON-RPC formatting errors.