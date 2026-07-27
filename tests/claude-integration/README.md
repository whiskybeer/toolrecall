# ToolRecall × Claude Code Integration Test

## What This Tests

Whether Claude Code uses ToolRecall's MCP `cached_read` instead of its native
`Read` tool when both are available. This determines if file caching actually
saves tokens for Claude Code users, or just adds tool schema overhead.

## How To Run

```bash
# 1. Ensure ToolRecall daemon is running
toolrecall daemon start

# 2. Ensure Claude Code is installed
claude --version

# 3. Run the test
cd tests/claude-integration
bash run-test.sh
```

The test:
1. Records pre-test cache stats from ToolRecall's SQLite DB
2. Runs Claude Code with a multi-turn task in this test directory
3. Records post-test cache stats
4. Reports whether cache hits occurred

## What The Result Means

| Result | Meaning |
|--------|---------|
| **PASS** | Claude Code called MCP `cached_read` — file caching works |
| **PARTIAL** | MCP tools called but all first-time reads (run again for repeat data) |
| **FAIL** | No MCP tool calls — Claude used native `Read` exclusively |
| **INCONCLUSIVE** | Data ambiguous — inspect the report manually |

## Test Design

The test project has multiple files (`src/main.js`, `src/config.js`,
`src/utils.js`, `test-file.txt`) that the prompt forces Claude to read and
modify multiple times across multiple turns, creating opportunities for
both cache hits and misses.

## Caveats

- Model routing varies by Claude Code version and model. Test with 3+ models.
- `-p` (non-interactive) mode may route differently than interactive sessions.
- MCP tool schemas add ~0.5-1K tokens to every turn — this is overhead you
  must subtract from any cache savings.