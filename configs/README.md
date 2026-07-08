# ToolRecall Agent Configs

Ready-to-use MCP configuration files for connecting ToolRecall to various AI coding agents.

## Prerequisite

`toolrecall` must be on PATH after installation:

```bash
pip install toolrecall
# or
pipx install toolrecall
```

## Quick Start (automatic)

```bash
toolrecall setup
```

This detects installed agents and wires up ToolRecall automatically — no manual file copying needed.

> ⚠️ **Claude Code users:** See [Agent Compatibility](../docs/AGENT_COMPATIBILITY.md) before configuring. File/terminal caching can cause stale-state issues in code edit loops.

## Manual Config Files

| File | Target Location | Agent |
|------|----------------|-------|
| `claude-code.json` | `~/.claude.json` (`mcpServers` section) | Claude Code ⚠️ |
| `cursor-mcp.json` | `~/.cursor/mcp.json` | Cursor |
| `cline-mcp.json` | Cline/Roo Code MCP config path | Cline / Roo Code |
| `opencode.jsonc` | `~/.opencode/opencode.jsonc` (`mcp` section) | OpenCode / Crush |
| `aider-mcp.json` | `~/.aider.mcp.json` or via `--mcp-toolrecall` | Aider |
| `windsurf-mcp.json` | Windsurf MCP config path | Windsurf |
| `continue-mcp.json` | Continue MCP config path | Continue |

### Claude Code — ⚠️ use with caution

See [Agent Compatibility](../docs/AGENT_COMPATIBILITY.md) before enabling. File/terminal caching can cause stale-state issues in code edit loops. These configs work safely for **MCP multiplex and forward proxy only**.

Two methods (automatic preferred):

1. **Automatic** — if `claude` binary is on PATH:
   ```bash
   claude mcp add toolrecall -s user -- toolrecall mcp
   ```
   This runs during `toolrecall setup` — no user action needed.

2. **Manual** — copy `claude-code.json` content into `~/.claude.json`:
   ```json
   {
     "mcpServers": {
       "toolrecall": {
         "command": "toolrecall",
         "args": ["mcp"]
       }
     }
   }
   ```

Also recommended: add to `~/.claude/claude_dotfiles/claude.md`:
```
## ToolRecall Cache
ToolRecall is installed. When reading files, use `cached_read` via MCP instead of `read_file`.
When running terminal commands, use `cached_terminal` instead of `terminal`.
```

### Cursor

Copy `cursor-mcp.json` to `~/.cursor/mcp.json`. Also add to `.cursorrules`:
```
Use cached_read for file reads (MCP tool, faster on repeats).
Use cached_terminal for terminal commands (MCP tool, TTL-cached).
```

### OpenCode / Crush (v1.17+)

Add to `~/.opencode/opencode.jsonc` under the `"mcp"` key:
```jsonc
{
  "mcp": {
    "toolrecall": {
      "type": "local",
      "command": "toolrecall",
      "args": ["mcp"],
      "enabled": true
    }
  }
}
```

### Hermes Agent

Hermes uses the **OS-level `.pth` shim** (`toolrecall/shim.py`) — no per-agent config needed.
The shim patches `builtins.open()` and `subprocess.run()` on every Python process, so
file reads and terminal commands are transparently cached for any Python-based agent.

```bash
toolrecall setup    # installs the .pth shim into site-packages
```

This enables **transparent caching** — native `read_file`/`terminal` are auto-cached without changing how the agent calls them.

### Cline / Roo Code

Add to `.clinerules`:
```
## ToolRecall Cache
When reading files, always use cached_read instead of read_file.
When running terminal commands, use cached_terminal.
```

### Any MCP-compatible agent

Add an MCP server with:
- **command:** `toolrecall`
- **args:** `["mcp"]`

## Test the Connection

```bash
# List available cache tools
echo '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' | toolrecall mcp

# Read a cached file
echo '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"cached_read","arguments":{"path":"/etc/os-release"}}}' | toolrecall mcp
```

## After Configuring

Make sure the daemon is running:
```bash
toolrecall daemon
toolrecall status
```