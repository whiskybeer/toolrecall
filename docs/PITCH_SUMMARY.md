# The Final Pitch

ToolRecall is a deterministic tool output cache for LLM agents. It sits between the agent and the OS, serving repeated tool calls from local SQLite instead of re-sending them to the LLM.

**What it does:**

1. **Local Token Reduction:** Repeated file reads, terminal commands, and MCP calls are served from SQLite at ~0.6ms instead of being re-sent. In measured benchmarks: ~55K tokens saved per 13-file workload, 67–97% hit rate depending on re-read depth. Roughly 81% fewer input tokens.

2. **Server-Side Discount Enablement:** ToolRecall returns byte-identical tool outputs until mtime/TTL expiry. This stabilizes the prompt prefix across turns, qualifying every API call for Anthropic/OpenAI's up-to-90% prefix caching discount. The local token savings are ~$6/session; the server-side discount is the larger cost lever.

3. **Determinism:** Same args + same mtime = same output. 100% reproducible agent runs, no OS flakiness.

4. **Security:** Zero-Trust WAF — `os.path.realpath` blocks directory traversal, `.env` files are air-gapped from the LLM, `allow_terminal=false` drops RCE attempts.

5. **Universal:** Standard `stdio` MCP (`toolrecall mcp`). Works with Claude Code, Cursor, Cline, Hermes, Aider — any MCP-speaking agent. No custom plugins needed.
