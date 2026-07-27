# AGENTS.md — ToolRecall Context Tracker Integration

Your session has ToolRecall's daemon running with the context tracker. The daemon
tracks which files are **dirty** (written/edited by you) vs **clean** (only read).
You can drop clean file content from your context every turn to keep the session
alive 7.4× longer.

## The Pattern

After every turn (user message → your response cycle):

1. **Before reading files for the next turn**, call the MCP tool
   `context_set_checkpoint` to mark your current state.

2. **Read files as normal** via `read_file` / `cached_read` MCP tools. The daemon
   tracks which files you read.

3. **When you edit/write files**, use `write_file` or `patch` MCP tools. The daemon
   marks them dirty.

4. **After completing work (after your response)**, call MCP tool `context_get_dirty`.
   It returns:
   ```json
   {"clean": ["path/to/file.py", ...], "dirty": ["path/to/edited.py", ...]}
   ```

5. **Scan your conversation for blocks matching `=== <path> === ... === end <path> ===`
   for every file in `clean`.** Remove those blocks. These are file contents the model
   has already seen — re-sending them wastes context window.

6. Call `context_set_checkpoint` again to start the next turn fresh.

## Example

```
After each response:
   1. Call context_get_dirty via MCP
   2. For each path in result.clean:
      - Find the matching === <path> === block in your conversation history
      - Remove the entire block (or replace with "// cached: <path>")
   3. Call context_set_checkpoint
```

## Why This Works

- **File cache** handles repeat reads (99% hit rate, ~0.6ms) — dropping and
  re-reading costs nothing
- **Context tracker** tells you exactly which files are safe to drop — never
  drop a dirty file you're actively editing
- **No quality loss**: clean files are unchanged since you read them
- **Bounded context**: only dirty files + instructions + your reasoning accumulate

## Stale Files

If a file changed on disk **after** you read it, the copy in your context is
out of date. Call `context_get_stale` to find them — these are correctness-critical
to evict or re-read, not just a token optimization.

For available MCP tools, call `tools/list` to see the full ToolRecall toolset.