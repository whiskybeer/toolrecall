# Agent Compatibility

Not all agents benefit equally from ToolRecall. This document explains **who wins, who loses, and how to configure each one correctly**.

---

## Quick Reference

| Agent | Caching Value | Use Shim? | Use MCP Bridge? | Notes |
|---|---|---|---|---|
| **Hermes** | ✅ High | ✅ Yes | ✅ Yes | Stateless, small context → biggest win |
| **OpenCode** | ✅ High | ❌ N/A (Node.js) | ✅ Yes | MCP multiplex is the killer feature |
| **Cline** | ✅ High | ✅ Yes | ✅ Yes | Benefits from both layers |
| **Aider** | ✅ Medium | ✅ Yes | ⚠️ Via `--mcp-toolrecall` | Aider is diff-patch based, fewer tool re-reads |
| **Claude Code** | ⚠️ **Low / Negative** | ❌ **Avoid** | ⚠️ Use with caution | See detailed section below |
| **Codex CLI** | ⚠️ Mixed | ❌ N/A (Node.js) | ⚠️ Multiplex only | MCP bridge for static tool multiplexing only |
| **Cursor** | ⚠️ Mixed | ⚠️ Shim safe | ⚠️ Configurable | Cursor manages its own tool state; MCP optional |

---

## Hermes (Nous Research) — ✅ Best-in-class

ToolRecall is **built into Hermes** — the tools `cached_read`, `cached_terminal`, `mcp_call` are available directly.

**Why it works:**
- Hermes is a stateless agent with limited context budget
- Repeated file reads and terminal calls inflate prompt size fast
- Deterministic cache → stable prompt prefixes → provider prefix-caching discount

**Config:**
```bash
pip install toolrecall && toolrecall setup
# Tools available natively in Hermes — no extra config needed.
```

---

## OpenCode — ✅ High value

OpenCode is a Node.js agent (shim doesn't apply), but the MCP bridge is transformative.

**Why it works:**
- OpenCode has no built-in MCP multiplexing — TR provides shared server subprocesses
- Lazy-loading avoids ~1.7s per-server startup on every session boot
- Shared daemon means GitHub / Postgres / etc. servers persist across OpenCode sessions

**Config:**
```jsonc
// ~/.opencode/opencode.jsonc
{
  "$schema": "https://opencode.ai/config.json",
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

`toolrecall setup` writes this automatically.

---

## Cline — ✅ High value

Cline is a Python agent that benefits from both the shim and MCP bridge.

**Config:** Add to `~/.config/cline/mcp_settings.json`:
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

The `.pth` shim auto-caches `open()` and `subprocess.run()` in every Cline session.

---

## Aider — ✅ Medium value

Aider's architecture is diff-patch based: it reads files, produces diffs, applies them. Fewer repeated tool calls than agents that iterate via tool loops.

**Config:**
```bash
aider --mcp-toolrecall
```

**Shim benefit:** The Python shim catches `read_file` calls from Aider's tool execution layer. Worth enabling for projects with large files read multiple times.

---

## Claude Code — ⚠️ Low / Negative value (use with caution)

ToolRecall's caching model **conflicts** with Claude Code's architecture in several ways:

| Issue | Why it happens |
|---|---|
| **Stale file state** | Claude Code reads a file, edits it, reads it again. An aggressive cache may serve the old content, causing Claude to "see" changes it already made — leading to edit loops or redundant fixes. |
| **Native state tracking** | Claude Code maintains its own in-memory task trees, directory snapshots, and recent terminal output. External caching is redundant at best, destructive at worst. |
| **Tool rejection errors** | Placing a proxy between Claude Code's stdio tool execution and the OS can trigger internal sanitization checks, causing unexpected tool execution failures. |
| **Node.js binary** | The Python shim `.pth` file doesn't affect Claude Code at all. |

### When *might* it still make sense?

- **MCP multiplex only** — if you run 5+ MCP servers (GitHub, Postgres, fetch, time), TR's multiplexer shares one subprocess per server across all sessions. Add TR as the *single MCP entry point* but **do not enable** `mcp_multiplex` servers that cache file content.
- **Forward proxy only** — caching API responses (OpenAI/Anthropic) via `:8569` is safe and orthogonal to Claude Code's tool loop.

> **Bottom line:** If you use Claude Code alone, you don't need ToolRecall. If you run Claude Code alongside Hermes/OpenCode, use TR only for the MCP multiplex and forward proxy — not for file/terminal caching.

---

## Codex CLI — ⚠️ Mixed

Codex CLI is Node.js (shim N/A). The MCP bridge is useful for multiplexing static tool servers.

**Config:** Use the MCP config format Codex CLI expects. ToolRecall acts as a multiplexing endpoint for read-only tools (time, fetch).

**Avoid:** Do not route file reading/editing through ToolRecall — Codex manages its own file state.

---

## Cursor — ⚠️ Mixed

Cursor has its own tool-execution plumbing. The shim is safe (Python process) and can help with file read caching, but Cursor manages its own state aggressively.

**Recommended:** Add MCP server for multiplex benefits, skip the shim for dynamic code files. Use `TOOLRECALL_SHIM_DISABLE=1` for Cursor's Python processes if you hit stale-state issues.

---

## Design Rationale

### Why stateless agents win

Open-source / stateless agent pipelines generally:
- Operate on smaller or more expensive context windows
- Have noticeable latency for tool execution (local models, self-hosted)
- Lack built-in MCP multiplexing

ToolRecall addresses all three: **smaller context** (cached tool outputs), **lower latency** (0.6ms vs 1.5s subprocess), **shared multiplexer** (one subprocess per MCP server for all agents).

### Why stateful agents push back

Proprietary terminal agents (Claude Code, Codex CLI) are heavily optimized around:
1. **Anti-idempotency** — coding environments are in constant flux; they *demand* live file state
2. **Native state tracking** — they compress and manage their own tool history within context
3. **Strict session monitoring** — stdio proxies can break internal error-handling and tool sanitization

### The verdict

| Architecture | Examples | ToolRecall Value |
|---|---|---|
| Stateless, Python, MCP-native | Hermes, Cline, Aider | ✅ High |
| Node.js, open models, no native MCP multiplex | OpenCode | ✅ High (MCP bridge) |
| Proprietary, stateful, self-tracking | Claude Code, Codex CLI | ⚠️ Low / Use selectively |
