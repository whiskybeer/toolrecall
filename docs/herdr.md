# ToolRecall + herdr (Terminal Multiplexer)

> **Date:** July 2026 · herdr v0.8+
> **Principle:** ToolRecall is agent-agnostic. Every agent in every herdr pane benefits — no per-pane config needed.

## Table of Contents

- [Overview](#overview)
- [Two Integration Paths](#two-integration-paths)
  - [1. tr Binary (Recommended — Universal)](#1-tr-binary-recommended--universal)
  - [2. MCP Bridge (For MCP-Capable Agents)](#2-mcp-bridge-for-mcp-capable-agents)
- [herdr + Hermes Agent](#herdr--hermes-agent)
- [Why Not a Plugin Yet?](#why-not-a-plugin-yet)
- [Comparison: Paths Side by Side](#comparison-paths-side-by-side)
- [Troubleshooting](#troubleshooting)
- [Future: Native herdr Plugin](#future-native-herdr-plugin)

---

## Overview

herdr is a Rust-based terminal multiplexer for running multiple AI coding agents in parallel panes. ToolRecall provides transparent caching for every agent running inside herdr — no per-agent or per-pane configuration needed.

herdr supports **21 agents** (Hermes, OpenCode, Claude Code, Codex, Copilot, Cursor, Kilo, Qoder, MastraCode, and more). ToolRecall works with **all of them** through two standard paths — no plugin, no Rust build, no herdr-side changes.

---

## Two Integration Paths

### 1. tr Binary (Recommended — Universal)

The `tr` Go binary connects to the ToolRecall daemon over UDS. Every agent in any herdr pane can shell out to it:

```bash
tr read path/to/file       # Cached file read
tr term "hostname"          # Cached terminal command
tr status                   # Cache stats
tr ping                     # Daemon health check
tr cat /etc/os-release      # Alias for read
tr write /tmp/test.txt ...  # Write (invalidates cache)
```

**Setup:**

```bash
# 1. Build the tr binary (requires Go)
cd toolrecall/go-client
go build -o /usr/local/bin/tr .

# 2. Ensure tr is on PATH in all herdr panes
# herdr typically inherits your shell PATH, so this is automatic

# 3. Start the ToolRecall daemon (once)
toolrecall daemon start

# Done. Every agent in every pane can now use tr.
```

**Works with:** Hermes, OpenCode, Claude Code, Codex, Cursor, Kilo, Qoder, MastraCode — any agent that can run a shell command.

### 2. MCP Bridge (For MCP-Capable Agents)

ToolRecall's MCP bridge exposes cached `read_file`, `write_file`, `patch`, and `terminal` tools. Add it to your agent's MCP config:

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

The MCP bridge also provides:
- **Context Tracker** (4 tools — checkpoint-based dirty-file tracking)
- **Multiplexed access** to built-in MCP servers (time, github, fetch, sequential-thinking)

For agents that support MCP discovery (Hermes, OpenCode), `toolrecall setup` writes this config automatically:

```bash
toolrecall setup
```

**Works with:** Hermes, OpenCode, Cline, Cursor, Windsurf, Continue — any MCP-compatible agent.

---

## herdr + Hermes Agent

Hermes Agent already has a native herdr integration (`herdr integration install hermes`) that writes a lifecycle plugin to `~/.hermes/plugins/herdr-agent-state/`. This is **herdr's integration**, not ToolRecall's — it handles session reporting and pane lifecycle.

When Hermes runs inside a herdr pane, ToolRecall's caching works via **both** paths:

- **tr binary** — Hermes shells out to `tr read` / `tr term` for file and terminal caching
- **MCP bridge** — Hermes connects to ToolRecall's MCP tools natively

No additional setup needed beyond `toolrecall setup` and `herdr integration install hermes`.

---

## Why Not a Plugin Yet?

herdr has a plugin system, but ToolRecall does **not** currently ship as a herdr plugin. The two paths above (tr + MCP) are standard ToolRecall features that work in any herdr pane today — no plugin, no Rust build, no herdr-side changes required.

A future native herdr plugin would auto-provision the `tr` binary and MCP config into new panes. Until then, the existing paths cover all agents.

---

## Comparison: Paths Side by Side

| Path | Setup | Caches | Works for |
|------|-------|--------|-----------|
| **tr binary** | `go build` once, put on PATH | File reads, terminal commands, MCP calls | Any agent in any pane |
| **MCP bridge** | `toolrecall setup` | Native-named cached tools, context tracker, multiplexer | MCP-capable agents |

Both paths work together. Use `tr` for quick shell commands and MCP for full agent integration. The same daemon serves both.

---

## Troubleshooting

| Problem | Likely Cause | Fix |
|---------|-------------|-----|
| `tr: command not found` | `tr` not on PATH | `go build -o /usr/local/bin/tr .` from `go-client/` |
| Daemon not responding | Daemon not started | `toolrecall daemon start` |
| MCP tools not showing up | Agent MCP config not updated | `toolrecall setup` or manually add to agent config |
| Agents in different panes don't share cache | They're running on different machines | All panes must be on the same machine (same daemon socket) |
| `tr read` returns stale data | Write was done outside cache | Use `tr read --bypass file.py` for fresh read, or `tr write` instead of raw shell writes |

---

## Future: Native herdr Plugin

Once herdr's plugin system is stable and ToolRecall's herdr integration is mature enough, ToolRecall will ship as a herdr plugin:

```bash
herdr plugin install toolrecall
```

This would auto-provision:
- The `tr` binary into every new pane
- MCP server config for MCP-capable agents
- Environment variables for all agents to discover ToolRecall

Track the issue: [github.com/whiskybeer/toolrecall/issues](https://github.com/whiskybeer/toolrecall/issues)