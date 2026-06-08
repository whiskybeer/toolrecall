---
name: toolrecall
description: "ToolRecall auto-caching: use cached_read, cached_terminal, cached_skill, cached_run, cached_exec instead of raw tools"
author: ToolRecall
tags: [cache, tokens, performance, hermes]
---

# ToolRecall Auto-Cache

**The L1 Cache and Middleware for LLM Agents.**

This skill configures an agent to aggressively use the `toolrecall` MCP server for tool executions. ToolRecall breaks the $O(N^2)$ context snowball by catching tool executions at the OS level and serving them from a local SQLite database in <0.1ms. 

By forcing the OS state to remain static for the agent, it forces 100% deterministic, byte-for-byte identical payloads. This guarantees massive Server-Side Cache hits at Anthropic/OpenAI (the 90% discount) and prevents OS jitter from breaking agent workflows.

### 🛠️ BEST PRACTICES FOR AGENTS (CONTEXT DROPPING)
Because ToolRecall makes fetching files and API calls virtually free and instant locally:
1. **Aggressively prune your context:** Do not hoard massive logs or codebases in your active memory. Read them, fix the bug, and DROP them from your context to save JSON payload size.
2. **Retrieve on demand:** If you need the file again 10 turns later, simply call `cached_read` again. ToolRecall will serve it to you instantly without hitting the physical disk.

### ⚠️ WHEN TO BYPASS THE CACHE (TTL=0)
ToolRecall intentionally freezes the world. If you are executing a task that *requires* you to observe real-time changes in the environment (e.g., polling a CI/CD server for a "SUCCESS" string every 10 seconds, or watching a log file for new entries):
* You **MUST** bypass the cache, otherwise you will be trapped in a phantom reality seeing the old frozen state.
* Use standard shell commands or explicitly pass `bypass_cache=true` (if using `mcp_call`) or `ttl=0` (if using `cached_terminal`).

This skill instructs the agent to use ToolRecall's cached variants of tools.

## Automatic Caching Rules

### 1. File Reads → `cached_read()`

```python
from toolrecall import cached_read

# Instead of: read_file('/path/to/file')
result = cached_read('/path/to/file')
```

- Automatically invalidated when file mtime changes
- No TTL needed — file system is the source of truth

### 2. Terminal Commands → `cached_terminal()`

```python
from toolrecall import cached_terminal

# Cache common commands with appropriate TTL
result = cached_terminal('git status', ttl=30)
result = cached_terminal('hostname', ttl=3600)
result = cached_terminal('free -h', ttl=300)
```

- Use TTL=30 for git status, TTL=3600 for hostname/uname
- Non-cacheable commands (unique commands) bypass cache automatically

### 3. Skills → `cached_skill()`

```python
from toolrecall import cached_skill

# Instead of: skill_view('skill-name')
skill = cached_skill('skill-name')
```

- Invalidated when any file in the skill directory changes

### 4. Script Execution → `cached_run()`

```python
from toolrecall import cached_run

# SAFE: Read-only analysis (safe to cache)
result = cached_run('scripts/analyze.py', '--input data.json', ttl=600)

# UNSAFE: State-changing script → disable cache with ttl=0
result = cached_run('deploy.sh', '--prod', ttl=0)    # Never cache deployments!
result = cached_run('migrate_db.py', '', ttl=0)       # Never cache DB migrations!
```

⚠️ **`ttl=0` disables caching entirely** — the script runs fresh every time.
Only use caching when the script is **read-only / idempotent**.

### 5. Python Code → `cached_exec()`

```python
from toolrecall import cached_exec

# SAFE: Idempotent computation (safe to cache)
stats = cached_exec('import pandas; print(df.describe())', ttl=300)
result = cached_exec('import json; print(json.dumps(data, indent=2))', ttl=60)

# UNSAFE: State-changing code → disable cache with ttl=0
result = cached_exec('import subprocess; subprocess.run(["git", "push"])', ttl=0)
```

## When NOT to cache

- Write operations (`git push`, `rm -rf`, `mv`, `cp`)
- State-changing commands
- Deployments, DB migrations, API calls
- **Use `ttl=0` to bypass cache** for state-changing scripts/code

## Checking Cache Stats

```python
from toolrecall.cache import get_stats
import json
print(json.dumps(get_stats(), indent=2))
```