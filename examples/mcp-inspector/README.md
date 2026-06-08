# Testing ToolRecall with the Official MCP Inspector

The [MCP Inspector](https://github.com/modelcontextprotocol/inspector) is the official interactive testing tool for MCP servers. It provides a React-based UI to test tools, resources, and prompts.

## Setup & Run

You don't need to install the inspector globally. You can run it directly via `npx` or `uvx`, pointing it to ToolRecall.

### Using NPX (Node)
```bash
npx @modelcontextprotocol/inspector toolrecall mcp
```

### Using UVX (Python)
If you prefer not to use Node, you can use the Python version of the inspector (if available) or stick to NPX.

## What to test in the Inspector UI:

1. **Connection**: The UI should show "Connected" immediately. ToolRecall auto-starts the daemon if it's not running.
2. **List Tools**: You should see all multiplexed tools from your configured upstream servers, PLUS ToolRecall's native tools (`cached_read`, `cached_skill`, `cache_status`, etc.).
3. **Execute Tool**: Run `cached_read` on a large file. 
   - First run: Takes normal time.
   - Second run: Instantaneous (served from SQLite WAL). Check the `cache_status` tool to see the hit count go up!