---
name: toolrecall
description: "ToolRecall auto-caching: use cached_read, cached_terminal, cached_skill, cached_run, cached_exec instead of raw tools"
author: ToolRecall
tags: [cache, tokens, performance, hermes]
---

# ToolRecall Auto-Cache

This skill instructs the agent to use ToolRecall's cached variants of tools to save tokens and time.

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