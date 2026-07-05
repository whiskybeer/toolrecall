# The Final Pitch

ToolRecall is a deterministic tool output cache for LLM agents. It sits between the agent and the OS, serving repeated tool calls from local SQLite instead of re-sending them to the LLM.

**What it does (cost):**

ToolRecall saves tokens through two separate mechanisms:

1. **Local deduplication** — repeated file reads are served from SQLite instead of re-executed. Measured: ~55K unique tokens cached per 13-file workload (73% fewer repeated file-read tokens). At $3/M input tokens: ~$0.17 per workload.

2. **Deterministic payloads (the real lever)** — byte-identical tool outputs stabilize the prompt prefix across turns, qualifying every API call for Anthropic/OpenAI's up-to-90% prefix caching discount.

**Example:** 1,000 API calls × 20K input tokens = 20M tokens.
Without TR: $60. With TR (90% discount): $6. Plus local dedup: ~$0.15.

3. **Determinism:** Same args + same mtime = same output. 100% reproducible agent runs, no OS flakiness.

4. **Security:** Zero-Trust WAF — `os.path.realpath` blocks directory traversal, `.env` files are air-gapped from the LLM, `allow_terminal=false` drops RCE attempts.

5. **Universal:** Standard `stdio` MCP (`toolrecall mcp`). Works with Hermes, OpenCode, Cline, Aider[^notall] — any MCP-speaking agent. No custom plugins needed.

[^notall]: Not all agents benefit equally. Claude Code and Codex CLI have native state tracking that can conflict with external caching. See [Agent Compatibility](AGENT_COMPATIBILITY.md) for per-agent guidance.
