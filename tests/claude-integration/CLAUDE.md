# ToolRecall Integration Test

This is a test project to verify whether Claude Code uses MCP `cached_read` tools
(provided by ToolRecall) instead of its native `Read` tool.

## Setup

ToolRecall daemon and MCP bridge are configured. The MCP server `toolrecall` is
registered and provides these tools alongside the default set:
- `read_file` / `cached_read`
- `write_file` / `cached_write`
- `patch` / `cached_patch`
- `terminal` / `cached_terminal`

## Your Task

You have TWO tool options for reading files: your **native Read** tool and the
**MCP `cached_read`** tool. This test measures which one you choose.

1. Read `src/config.js` and tell me what port the server runs on.
2. Read `src/utils.js` and tell me how many utility functions it exports.
3. Read `src/main.js` and tell me what endpoints it registers.
4. Edit `src/config.js` to change the port from 3000 to 4000.
5. Read `src/config.js` again to verify the change took effect.
6. Edit `test-file.txt` and append a line with the current timestamp.
7. Read `test-file.txt` again to verify the append worked.

## What We're Measuring

The ToolRecall daemon is tracking every file read in its SQLite cache DB.
After this test completes, a script will check whether the reads were served
from the MCP cache or went through your native Read tool.

- **If cache hit:** You used MCP `cached_read` → ToolRecall file caching is viable
- **If no cache hit:** You used native `Read` exclusively → ToolRecall adds tool schemas for no gain

There are no wrong answers. The data is what it is.
