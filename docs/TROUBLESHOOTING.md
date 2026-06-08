# Troubleshooting & FAQ

If you encounter issues while running ToolRecall, check this guide.

## 1. Daemon won't start or MCP calls hang
**Symptom:** `toolrecall mcp` hangs, or `toolrecall status` returns connection refused.
**Cause:** The daemon is not running, or the Unix Domain Socket file is stale/permissions are wrong.
**Fix:**
```bash
# 1. Kill the daemon and any hanging python processes
toolrecall daemon --stop
pkill -f "toolrecall daemon"

# 2. Start it in the foreground to see actual errors
toolrecall daemon --foreground
```

## 2. "Access Denied" when reading files
**Symptom:** Your agent tries to read a file but gets an error: `Access Denied: Path not in allowed_paths`.
**Cause:** ToolRecall's WAF (Security Sandbox) is blocking the read to prevent prompt injection.
**Fix:** Edit your `~/.toolrecall/config.toml` and add the required directory to `allowed_paths`.
```toml
[mcp]
allowed_paths = [
    "~/projects/my-new-app",
    "~/.hermes/skills"
]
```
*(Note: You must restart the daemon after changing the config)*

## 3. Terminal commands are not executing
**Symptom:** The agent tries to run `git status`, but it is blocked.
**Cause:** By default, shell execution is strictly disabled.
**Fix:** Set `allow_terminal = true` in `~/.toolrecall/config.toml`.

## 4. MCP Server is returning outdated data
**Symptom:** You merged a PR on GitHub, but the agent still sees it as "Open".
**Cause:** The MCP call was cached via the Multiplexer, and the TTL hasn't expired.
**Fix:** You can either wait for the TTL to expire, or force-clear the entire cache:
```bash
toolrecall invalidate
```

## 5. High RAM usage
**Symptom:** The daemon is consuming 500MB+ RAM.
**Cause:** You recently queried multiple heavy Node.js MCP servers. 
**Fix:** Do nothing. The MCP Multiplexer uses Lazy Loading. It keeps servers alive for 15 minutes to serve subsequent requests instantly. After 15 minutes of idle time, it will automatically kill the Node processes, and RAM usage will drop back to ~11MB.
