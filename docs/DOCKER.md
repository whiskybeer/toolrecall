# ToolRecall Docker — Containerized Cache & MCP Infrastructure

## Quick Start

```bash
# 1. Build and start the daemon + proxy
docker compose up -d daemon proxy

# 2. Check health (proxy exposes HTTP on port 8569; daemon itself speaks UDS only)
docker compose ps
curl http://localhost:8569/health

# 3. Mount your Hermes skills/projects as knowledge sources
# Edit docker-compose.yml or set PROJECTS_DIR:
PROJECTS_DIR=/home/user/projects docker compose up -d daemon
```

## Services

| Service | Image Target | Port | Description |
|---------|-------------|------|-------------|
| `daemon` | `daemon` | — | Cache + MCP multiplexer (UDS only) |
| `proxy` | `proxy` | 8569 | HTTP proxy (standalone) |
| `mcp-bridge` | `mcp-bridge` | — | MCP stdio bridge (stdin/stdout) |
| `with-ollama` | `with-ollama` | 8569+11434 | Full stack + local LLM |
| `full` | `full` | 8569 | Daemon + proxy via supervisor |

## Configuration

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `INDEX_MEMORY` | `true` | Auto-index Hermes memory on start |
| `ENABLE_PROXY` | `true` | Start HTTP proxy alongside daemon |
| `ENABLE_OLLAMA` | `false` | Start Ollama for local LLM inference |
| `OLLAMA_MODEL` | `llama3.2:1b` | Model to pull on first start |
| `TOOLRECALL_SCAN_DIRS` | — | Directories to scan for knowledge indexing |
| `TOOLRECALL_DATA` | `/data` | Persistent data directory |

### Volumes

| Mount | Purpose |
|-------|---------|
| `toolrecall_data:/data` | Persistent cache + knowledge DB |
| `./projects:/projects:ro` | Project files for knowledge indexing (read-only) |
| `ollama_models:/root/.ollama` | Ollama model storage (with-ollama only) |

## Full Stack with Local LLM (Target Group: Offline Devs)

The `with-ollama` service bundles ToolRecall with **Ollama** for fully
offline, air-gapped AI development:

```bash
# Start with a small model (1B, fits 2GB RAM)
OLLAMA_MODEL=llama3.2:1b docker compose --profile ollama up -d with-ollama

# Start with a larger model (requires GPU)
OLLAMA_MODEL=deepseek-r1:8b docker compose --profile ollama up -d with-ollama
```

**Why?** For developers who:
- Work in air-gapped / offline environments
- Need privacy (no data leaves the machine)
- Want a self-contained stack (no API keys, no subscriptions)
- Are prototyping / don't yet need production LLM throughput

**Minimal requirements:**
- `llama3.2:1b` → 2GB RAM, CPU-only (no GPU needed)
- `deepseek-r1:8b` → 8GB RAM, GPU recommended
- `llama3.2:3b` → 4GB RAM, works on any modern laptop

## Deployment Examples

### Minimal (daemon only)
```bash
docker compose up -d daemon
```
→ 11MB RAM idle, connects via UDS.

### Standard (daemon + proxy)
```bash
docker compose up -d proxy
```
→ Auto-starts daemon dependency. HTTP on :8569.

### Offline Dev Stack (ToolRecall + Ollama)
```bash
OLLAMA_MODEL=llama3.2:1b docker compose --profile ollama up -d with-ollama
```
→ 2 services, ~600MB RAM total, fully offline.

### Supervisor (all-in-one process)
```bash
docker build --target full -t toolrecall:full .
docker run -v toolrecall_data:/data -p 8569:8569 toolrecall:full
```

---

## 🔒 Security Gate in Docker & Kubernetes

### Problem: WAF Configuration in Containers

ToolRecall's Security Gate (`SecurityGate` / WAF) controls which paths,
tools, and terminal commands an LLM agent can execute. Inside a container
**default paths and permissions differ** from the host:

```toml
# ~/.config/toolrecall/toolrecall.toml — Important for Docker!
[mcp]
# Container paths instead of ~/.hermes/...
allowed_paths = [
    "/data",        # Persistent Volume
    "/projects",    # Read-only project mount
]
allow_terminal = false   # ❌ Especially critical in containers!
allow_invalidate = false

[security]
tool_access_control = false  # MCP keyword access control (not OS sandbox)
```

### Common Pitfalls (Docker)

| Symptom | Cause | Fix |
|---------|-------|-----|
| `Access Denied: Path not allowed` | `allowed_paths` points to host paths (`~/.hermes/`) that don't exist inside the container | Use container paths (`/data`, `/projects`) |
| Terminal commands hang | `allow_terminal=true` spawns subprocesses without TTY inside container | Set `allow_terminal=false`; use read-only MCP tools |
| Daemon won't start | UDS socket path doesn't exist (volume not mounted) | Set `TOOLRECALL_UDS_PATH=/data/tc.sock`, mount `/data` as volume |
| Cache lost on restart | No persistent volume | `volumes: toolrecall_data:/data` in compose |

### Kubernetes (Pod Security)

```yaml
# Security Context for ToolRecall Pod
apiVersion: v1
kind: Pod
spec:
  containers:
  - name: toolrecall-daemon
    securityContext:
      readOnlyRootFilesystem: true    # 🛡️ WAF layer
      runAsNonRoot: true
      capabilities:
        drop: ["ALL"]
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
    - name: projects
      mountPath: /projects
      readOnly: true
```

### Security Gate is Active Inside Containers

The WAF (`SecurityGate`) runs **inside the daemon process** and is not
affected by the host OS. Important for Docker/K8s:

| Gate | Effect | Docker Implication |
|------|--------|-------------------|
| **Path Canonicalization** (`os.path.realpath`) | Blocks directory traversal | Works **inside container FS** — relative paths to host are impossible |
| **Null Byte Poisoning** | Catches `valid.png%00/etc/shadow` | Container has no access to host `/etc/shadow` — additional safety |
| **MCP Keyword Access Control** | Blocks tools named `write`, `delete`, `exec` (substring match) | Recommended (default off): `security.tool_access_control = true` — **not OS sandbox** |
| **Terminal Block** | `allow_terminal=false` (default) | Especially important: prevent container shell access via agent |
| **Dangerous Tool Detection** | Blocks tools with `write`, `delete`, `exec` in name | Compiles with read-only sandbox for K8s deployments |

### Recommended Config for Kubernetes

```toml
# ~/.config/toolrecall/toolrecall.toml — K8s-optimized
[security]
tool_access_control = true

[mcp]
allowed_paths = ["/data", "/projects"]
allow_terminal = false
allow_invalidate = false

[sources.memory]
enabled = false  # Hermes memory doesn't exist inside the container
```

---

## Deployment Examples

## Health Check

```bash
# Via UDS (inside container)
python3 -c "
import os, socket, json
s = socket.socket(socket.AF_UNIX)
s.settimeout(3)
s.connect(os.environ['TOOLRECALL_UDS_PATH'])
s.sendall(json.dumps({'action': 'ping'}).encode())
print(s.recv(4096).decode())
s.close()
"
# → {"status": "pong"}

# Via HTTP (only works if proxy container is running and port 8569 is exposed)
curl http://localhost:8569/health
# → {"status": "ok", ...}
```

## Building Without Docker Compose

```bash
# Daemon only
docker build --target daemon -t toolrecall:daemon .
docker run -d -v toolrecall_data:/data toolrecall:daemon

# Full stack (daemon + proxy via supervisor)
docker build --target full -t toolrecall:full .
docker run -d \
  -v toolrecall_data:/data \
  -v ./projects:/projects:ro \
  -p 8569:8569 \
  toolrecall:full

# With Ollama (for offline inference)
docker build --target with-ollama -t toolrecall:ollama .
docker run -d \
  -v toolrecall_data:/data \
  -v ollama_models:/root/.ollama \
  -p 8569:8569 \
  -p 11434:11434 \
  -e ENABLE_OLLAMA=true \
  -e OLLAMA_MODEL=llama3.2:1b \
  toolrecall:ollama
```