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

## 6. Docker: "Access Denied" despite allowed path
**Symptom:** Agent receives `Access Denied: Path not allowed` when reading `/projects/my-code`, even though the path is listed in `allowed_paths`.
**Cause:** `allowed_paths` points to host paths (`~/.hermes/`, `/home/user/projects`) that **do not exist inside the container**. The container has its own filesystem.
**Fix:** Point config to container paths (see `docs/DOCKER.md`):
```toml
[mcp]
allowed_paths = ["/data", "/projects"]
```
**Best practice:** Always use `docker compose config` or `docker inspect` to verify actual mount paths.

## 7. Docker: Container won't start — "socket file not found"
**Symptom:** `docker compose logs daemon` shows: `OSError: [Errno 2] No such file or directory` for the UDS socket.
**Cause:** The daemon tries to create the socket in a path that isn't mounted as a volume. By default, ToolRecall expects `~/.toolrecall/` on the host.
**Fix:** Set `TOOLRECALL_UDS_PATH` to the data volume:
```yaml
environment:
  - TOOLRECALL_UDS_PATH=/data/tc.sock
volumes:
  - toolrecall_data:/data
```

## 8. Docker: allow_terminal=true hangs (no TTY)
**Symptom:** Terminal commands in Docker container hang or timeout.
**Cause:** `allow_terminal=true` spawns shell processes that expect a TTY. Inside a container there is no TTY — no STDIN or real shell environment.
**Fix:**
- `allow_terminal=false` (default) — avoid terminal via MCP
- Or start container with `tty: true`:
```yaml
services:
  daemon:
    tty: true
    stdin_open: true
```
**⚠️ Security risk:** Terminal access inside container = potential container breakout. Only enable in dev environments.

## 9. Kubernetes: Pod restarts due to security context
**Symptom:** ToolRecall Pod crash-loops with `readOnlyRootFilesystem: true`.
**Cause:** ToolRecall writes cache + knowledge DB + UDS socket. With `readOnlyRootFilesystem: true`, only mounted volumes are writable.
**Fix:** Mount all write paths as volumes:
```yaml
env:
- name: TOOLRECALL_CACHE_DB
  value: /data/cache.db
- name: TOOLRECALL_KNOWLEDGE_DB
  value: /data/knowledge.db
- name: TOOLRECALL_UDS_PATH
  value: /data/tc.sock
volumeMounts:
- name: data
  mountPath: /data
```

## 10. Security Gate: How to verify it's active
**Symptom:** Unsure whether the WAF is active inside the Docker container.
**Check:**
```bash
# 1. Check logs
docker compose logs daemon | grep -i 'security\\|waf\\|gate\\|sandbox\\|allowed'

# 2. Daemon status (shows config)
toolrecall daemon --status

# 3. Test: Directory traversal blocked?
curl -s 'http://localhost:8567/cached_read?path=../../etc/shadow'
# → Should return "Access Denied", not host content
```
**Explanation:** Security Gate runs **inside the daemon process**, independent of the host OS. As long as the daemon runs in the container, the WAF is active. The host's `/etc/shadow` is invisible anyway — container isolation + WAF = defense in depth.
