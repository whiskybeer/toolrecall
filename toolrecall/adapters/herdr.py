"""
Herdr Integration Guide — ToolRecall with the herdr terminal multiplexer.

herdr is a Rust-based terminal multiplexer for running multiple AI coding
agents in parallel panes. ToolRecall provides transparent caching for
every agent running inside herdr — no per-agent config needed.

Two integration paths, both working today:

1. tr binary (Go client) — Universal, any agent, any language
2. MCP bridge — For MCP-capable agents, native-named cached tools

Neither path requires a Rust build pipeline, a herdr plugin, or any
changes to herdr itself. These are standard ToolRecall features that
agents in herdr panes can use immediately.

Path 1: tr Binary (Recommended — Universal)
---------------------------------------------
The tr Go binary connects to the ToolRecall daemon over UDS.
Every agent in any herdr pane can shell out to tr:

    tr read path/to/file       # Cached file read
    tr term "hostname"          # Cached terminal command
    tr status                   # Cache stats
    tr ping                     # Daemon health check

Setup:
    # Build or download tr (see go-client/README.md)
    cd toolrecall/go-client
    go build -o /usr/local/bin/tr .

    # Ensure tr is on PATH in all herdr panes
    # (herdr typically inherits your shell PATH)

    # Start the ToolRecall daemon
    toolrecall daemon start

    # Done. Every agent in every pane can now use tr.

Path 2: MCP Bridge (For MCP-Capable Agents)
---------------------------------------------
ToolRecall's MCP bridge exposes cached read_file, write_file, patch,
and terminal tools. Add it to your agent's MCP config:

    {
        "mcpServers": {
            "toolrecall": {
                "command": "toolrecall",
                "args": ["mcp"]
            }
        }
    }

The MCP bridge also provides the Context Tracker (4 tools) and
multiplexed access to built-in MCP servers (time, github, fetch).

For agents that support MCP discovery (like Hermes and OpenCode),
toolrecall setup writes this config automatically:

    toolrecall setup

Both agents in separate herdr panes share the same ToolRecall daemon
and the same SQLite cache. What one agent caches, the other can hit.

Comparison
----------
| Path | Setup | Caches | Works for |
|------|-------|--------|-----------|
| tr binary | Build once, put on PATH | File reads, terminal commands, MCP calls | Any agent in any pane |
| MCP bridge | toolrecall setup | Native-named cached tools, context tracker, multiplexer | MCP-capable agents |

Both paths work together. Use tr for quick shell commands and MCP
for full agent integration. The same daemon serves both.

Future: herdr Plugin
--------------------
Once herdr's plugin system is stable, ToolRecall will ship as a
herdr plugin (herdr plugin install toolrecall) that auto-provisions
the tr binary and MCP config into new panes. Track the issue:
https://github.com/whiskybeer/toolrecall/issues
"""

# This module is documentation-only.
# The actual integration is the tr binary and MCP bridge - both
# already exist and work with herdr out of the box.

__all__ = []

import logging

logger = logging.getLogger("toolrecall.adapters.herdr")


def setup_notice():
    """Print a one-time setup notice for herdr users.

    Call this in your shell profile or agent init script to show
    the available integration paths.
    """
    print(
        """ToolRecall + herdr Integration
===============================
Two ways to cache tool calls in herdr panes:

  tr read|term|status  —  Universal Go binary (any agent, any language)
  toolrecall mcp       —  MCP bridge (native cached tools)

See toolrecall/adapters/herdr.py for full setup guide.
"""
    )