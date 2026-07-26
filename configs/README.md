# ToolRecall MCP Config Templates

Config templates for non-Python agents (Claude Code, Cursor, Cline, Windsurf,
Continue, Aider). These agents are not Python processes, so the `.pth` shim
doesn't apply. They access the cache through the MCP bridge (`toolrecall mcp`)
which connects to the daemon over UDS.

## Important Limitations

### Context Tracker (Endurance) — NOT available in append-only harnesses

The **Context Tracker** pattern requires the agent to drop clean file content
from its context window after each turn:

    context_set_checkpoint → read files → work → context_get_dirty → drop clean files → ...

**Claude Code, Cursor, and other append-only harnesses cannot do this.** Their
transcripts are append-only from the model's side; only harness-level compaction
removes content. The model can call `context_get_dirty` and receive the droppable
list, but has no mechanism to act on it.

This means:
- The **7.4× endurance gain** (140 turns vs 17) documented in BENCHMARK.md does
  **NOT** transfer to Claude Code or Cursor.
- The net effect of adding ToolRecall's MCP server in these environments is
  plausibly **neutral to slightly negative**: added tool schemas on every turn
  plus extra `cached_read` round-trips, with zero endurance gain.
- The **forward proxy** (caching API responses) and **MCP multiplexer** (shared
  server subprocesses) are still beneficial and are the recommended features for
  these agents.
