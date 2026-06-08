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

## 6. Docker: "Access Denied" obwohl Pfad erlaubt ist
**Symptom:** Der Agent bekommt `Access Denied: Path not allowed` beim Lesen von `/projects/mein-code`, obwohl der Pfad in `allowed_paths` steht.
**Ursache:** `allowed_paths` zeigt auf Host-Pfade (`~/.hermes/`, `/home/user/projects`), die **im Container nicht existieren**. Der Container hat ein eigenes Filesystem.
**Fix:** Config auf Container-Pfade umstellen (siehe `docs/DOCKER.md`):
```toml
[mcp]
allowed_paths = ["/data", "/projects"]
```
**Vorsorge:** Immer `docker compose config` oder `docker inspect` nutzen, um die tatsächlichen Mount-Pfade zu prüfen.

## 7. Docker: Container startet nicht — "socket file not found"
**Symptom:** `docker compose logs daemon` zeigt: `OSError: [Errno 2] No such file or directory` beim UDS-Socket.
**Ursache:** Der Daemon versucht, den Socket in einem Pfad zu erstellen, der nicht als Volume gemounted ist. Standardmäßig erwartet ToolRecall `~/.toolrecall/` auf dem Host.
**Fix:** TOOLRECALL_UDS_PATH auf das Data-Volume setzen:
```yaml
environment:
  - TOOLRECALL_UDS_PATH=/data/tc.sock
volumes:
  - toolrecall_data:/data
```

## 8. Docker: allow_terminal=true hängt (kein TTY)
**Symptom:** Terminal-Kommandos im Docker-Container hängen oder Timeout.
**Ursache:** `allow_terminal=true` startet Shell-Prozesse, die ein TTY erwarten. Im Container gibt es kein TTY — weder STDIN noch eine echte Shell-Umgebung.
**Fix:** 
- `allow_terminal=false` (default) — Terminal über MCP vermeiden
- Oder Container mit `tty: true` starten:
```yaml
services:
  daemon:
    tty: true
    stdin_open: true
```
**⚠️ Sicherheitsrisiko:** Terminal-Zugriff im Container = potenzieller Container-Breakout. Nur in Dev-Umgebungen aktivieren.

## 9. Kubernetes: Pod wird wegen Security Context neugestartet
**Symptom:** ToolRecall Pod crash-loopt mit `readOnlyRootFilesystem: true`.
**Ursache:** ToolRecall schreibt Cache + Knowledge DB + UDS Socket. Bei `readOnlyRootFilesystem: true` darf nur in gemountete Volumes geschrieben werden.
**Fix:** Alle Schreibpfade explizit als Volumes mounten:
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

## 10. Security Gate: Woher weiß ich, ob es aktiv ist?
**Symptom:** Ich bin unsicher, ob die WAF im Docker-Container greift.
**Check:**
```bash
# 1. Logs prüfen
docker compose logs daemon | grep -i 'security\|waf\|gate\|sandbox\|allowed'

# 2. Daemon-Status (zeigt Config an)
toolrecall daemon --status

# 3. Test: Directory Traversal blockiert?
curl -s 'http://localhost:8567/cached_read?path=../../etc/shadow'
# → Sollte "Access Denied" zurückgeben, nicht den Host-Inhalt
```
**Erklärung:** Security Gate läuft **im Daemon-Prozess**, unabhängig vom Host-OS. Solange der Daemon im Container läuft, ist die WAF aktiv. Die Host-`/etc/shadow` ist ohnehin unsichtbar — Container-Isolation + WAF = Defence in Depth.
