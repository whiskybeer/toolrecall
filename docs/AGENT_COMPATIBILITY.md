# Agent Compatibility

Not all agents benefit equally from ToolRecall. This document explains **who wins, who loses, and how to configure each one correctly**.

---

## Decision Table

Pick your agent and integration layer. The table tells you what value to expect and any caveats.

| Agent | MCP Bridge | Forward Proxy | Shim | Value | Notes |
|-------|-----------|---------------|------|-------|-------|
| **Hermes** | ✅ | ✅ | ✅ | **High** | Built-in native. Stateless, small context — biggest win. Context Tracker auto-hint after every tool call. |
| **OpenCode** | ✅ | ✅ | ❌ N/A (Node.js) | **High** | MCP multiplex is the killer feature. |
| **Cline** | ✅ | ✅ | ✅ | **High** | Benefits from both MCP bridge and shim. |
| **Aider** | ✅ Via `--mcp-toolrecall` | ✅ | ✅ | **Medium** | Diff-patch based, fewer tool re-reads. |
| **Google ADK** | ✅ | ✅ | ✅ | **High** | Python SDK, no built-in tool caching; shim catches `open()` in tools. |
| **Claude Code** | ⚠️ Multiplex only | ✅ | ❌ N/A | **Selective** | MCP bridge for multiplex only — do NOT enable file/terminal caching. Forward proxy is safe and orthogonal. |
| **Codex CLI** | ⚠️ Multiplex only | ✅ | ❌ N/A (Node.js) | **Selective** | MCP bridge for static tool multiplexing only. |
| **Cursor** | ⚠️ Optional | ✅ | ⚠️ Safe but redundant | **Low** | Cursor manages its own tool state. |

---

## Hermes (Nous Research) — ✅ Best-in-class

ToolRecall is **built into Hermes** — the tools `read_file`, `terminal`, `mcp_call` are available directly (native MCP names). The internal daemon commands are `cached_read`, `cached_terminal`, etc. — both names work in the MCP bridge.

**Why it works:**
- Hermes is a stateless agent with limited context budget
- Repeated file reads and terminal calls inflate prompt size fast
- Context Tracker provides per-turn hints for which files to drop

**Config:**
```bash
pipx install toolrecall && toolrecall setup
# or: uv tool install toolrecall && toolrecall setup
# Tools available natively in Hermes — no extra config needed.
```

> **⚠️  Shim venv:** If you used `pipx` or `uv tool install`, `toolrecall setup` auto-detects
> the Hermes venv and installs the `.pth` shim there. If this fails, or you skipped `setup`,
> run manually:
> ```bash
> toolrecall shim --install --venv ~/.hermes/hermes-agent/venv
> ```

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

The `.pth` shim auto-caches `open()`, `subprocess.run()`, and `subprocess.Popen()` in every Cline session.

---

## Aider — ✅ Medium value

Aider's architecture is diff-patch based: it reads files, produces diffs, applies them. Fewer repeated tool calls than agents that iterate via tool loops.

**Config:**
```bash
aider --mcp-toolrecall
```

**Shim benefit:** The Python shim catches `read_file` calls from Aider's tool execution layer. Worth enabling for projects with large files read multiple times.

---

## Google ADK — ✅ High value

Google's Agent Development Kit (ADK) is a Python framework with no built-in tool-output caching. Every tool call runs through `run_async()` fresh — even repeated reads of the same file.

**Why it works:**
- ADK tools are plain Python functions, so the `.pth` shim transparently caches `open()`, `subprocess.run()`, and `subprocess.Popen()` calls inside them.
- The MCP Bridge gives ADK access to shared, persistent MCP server subprocesses (time, fetch, GitHub, etc.).
- For deepest integration, wrap tools with `cached_read`, `cached_write`, etc.

**Config:**
```bash
pipx install toolrecall && toolrecall setup
# No per-agent config needed for the shim.
```

For detailed ADK-specific patterns, see [ToolRecall + Google ADK](google-adk.md).

---

## Claude Code — ⚠️ Use selectively

ToolRecall and Claude Code have different strengths. The cache adds little value for Claude Code's workflow, but two features are safe and useful:

**What works:**
- **Forward proxy** — caching API responses via `:8569` is orthogonal to Claude Code's tool loop and saves real cost.
- **MCP multiplexer only** — if you run 5+ MCP servers (GitHub, Postgres, fetch, time), TR's multiplexer shares one subprocess per server across all sessions. Add TR as the single MCP entry point.

**What to avoid:**
- **File/terminal caching** — Claude Code maintains its own in-memory state tracking. External caching adds staleness risk with no benefit.
- **Python shim** — Claude Code is Node.js; the shim doesn't apply.

**Config:**
```json
// ~/.claude/settings.json
{
  "mcpServers": {
    "toolrecall": {
      "command": "toolrecall",
      "args": ["mcp"]
    }
  }
}
```

> **Bottom line:** Add TR for the forward proxy and MCP multiplex benefits. Skip file/terminal caching — Claude Code manages those itself.

---

## Codex CLI — ⚠️ Use selectively

Codex CLI is Node.js (shim N/A). The MCP bridge is useful for multiplexing static tool servers.

**Config:** Use the MCP config format Codex CLI expects. ToolRecall acts as a multiplexing endpoint for read-only tools (time, fetch).

**Avoid:** Do not route file reading/editing through ToolRecall — Codex manages its own file state.

---

## Cursor — ⚠️ Low value

Cursor has its own tool-execution plumbing. The shim is safe (Python process) but largely redundant — Cursor manages its own state aggressively.

**Recommended:** Skip ToolRecall for Cursor sessions. The forward proxy is the only feature that adds value (API cost savings).

---

## Integration Layer Reference

| Layer | What it does | Requires | Best for |
|-------|-------------|----------|----------|
| **MCP Bridge** (`toolrecall mcp`) | Single MCP entry point → daemon → multiplexed servers + caching | MCP-compatible agent | Any agent. The default. |
| **Forward Proxy** (`:8569`) | Caches API responses by body hash | SDK pointed at `http://localhost:8569` | Any agent making API calls. Saves $ in dev loops. |
| **Python Shim** (`.pth` file) | Transparently caches `open()`, `subprocess.run()`, and `subprocess.Popen()`, auto-strips agent shell wrappers | Python agent, pipx or `toolrecall shim --install` | Python agents without native TR support. Marked experimental. |
| **Go Client** (`tr` binary) | Direct UDS connection to daemon | `go build` in `go-client/` | Shell scripts, CI/CD, non-Python agents, herdr panes. |