# ToolRecall Docker — Containerized Cache & MCP Infrastructure

## Quick Start

```bash
# 1. Build and start the daemon + proxy
docker compose up -d daemon proxy

# 2. Check health
docker compose ps
curl http://localhost:8567/health

# 3. Mount your Hermes skills/projects as knowledge sources
# Edit docker-compose.yml or set PROJECTS_DIR:
PROJECTS_DIR=/home/user/projects docker compose up -d daemon
```

## Services

| Service | Image Target | Port | Description |
|---------|-------------|------|-------------|
| `daemon` | `daemon` | 8567 | Cache + MCP multiplexer (UDS) |
| `proxy` | `proxy` | 8567 | HTTP proxy (standalone) |
| `mcp-bridge` | `mcp-bridge` | — | MCP stdio bridge (stdin/stdout) |
| `with-ollama` | `with-ollama` | 8567+11434 | Full stack + local LLM |
| `full` | `full` | 8567 | Daemon + proxy via supervisor |

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
→ Auto-starts daemon dependency. HTTP on :8567.

### Offline Dev Stack (ToolRecall + Ollama)
```bash
OLLAMA_MODEL=llama3.2:1b docker compose --profile ollama up -d with-ollama
```
→ 2 services, ~600MB RAM total, fully offline.

### Supervisor (all-in-one process)
```bash
docker build --target full -t toolrecall:full .
docker run -v toolrecall_data:/data -p 8567:8567 toolrecall:full
```

---

## 🔒 Security Gate in Docker & Kubernetes

### Problem: WAF-Konfiguration im Container

ToolRecalls Security Gate (`SecurityGate` / WAF) kontrolliert, welche Pfade,
Tools und Terminal-Kommandos ein LLM-Agent ausführen darf. In einem Container
sind **default paths und Berechtigungen anders** als auf dem Host:

```toml
# ~/.toolrecall/config.toml — Wichtig für Docker!
[mcp]
# Container-Pfade statt ~/.hermes/...:
allowed_paths = [
    "/data",        # Persistent Volume
    "/projects",    # Read-only Projekt-Mount
]
allow_terminal = false   # ❌ Im Container besonders kritisch!
allow_invalidate = false

[security]
read_only_sandbox = false  # ✅ Empfohlen für Docker: true = read-only
```

### Typische Fallstricke (Docker)

| Symptom | Ursache | Fix |
|---------|---------|-----|
| `Access Denied: Path not allowed` | `allowed_paths` zeigt auf Host-Pfade (`~/.hermes/`) die im Container nicht existieren | Auf Container-Pfade umstellen (`/data`, `/projects`) |
| Terminal-Kommandos hängen | `allow_terminal=true` startet Subprozesse ohne TTY im Container | `allow_terminal=false` setzen; nur lesende MCP-Tools nutzen |
| Daemon startet nicht | UDS-Socket-Pfad existiert nicht (Volume nicht gemountet) | `TOOLRECALL_UDS_PATH=/data/tc.sock` setzen, `/data` als Volume |
| Cache wird bei Neustart gelöscht | Kein persistent Volume | `volumes: toolrecall_data:/data` im Compose |

### Kubernetes (Pod Security)

```yaml
# Security Context für ToolRecall Pod
apiVersion: v1
kind: Pod
spec:
  containers:
  - name: toolrecall-daemon
    securityContext:
      readOnlyRootFilesystem: true    # 🛡️ WAF-Ebene
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

### Security Gate ist auch im Container aktiv

Das WAF (`SecurityGate`) läuft **innerhalb des Daemon-Prozesses** und wird
nicht vom Host-OS beeinflusst. Wichtig für Docker/K8s:

| Gate | Wirkung | Docker-Implikation |
|------|---------|-------------------|
| **Path Canonicalization** (`os.path.realpath`) | Blockiert Directory Traversal | Funktioniert **innerhalb des Container-FS** — relative Pfade zum Host sind unmöglich |
| **Null Byte Poisoning** | `valid.png%00/etc/shadow` wird erkannt | Container hat kein Zugriff auf Host-`/etc/shadow` — zusätzliche Sicherheit |
| **Read-Only Sandbox** | Blockiert alle schreibenden Tools | Empfohlen (default off): `security.read_only_sandbox = true` |
| **Terminal Block** | `allow_terminal=false` (default) | Besonders wichtig: Container-Shell-Zugriff via Agent verhindern |
| **Dangerous Tool Detection** | Blockiert Tools mit `write`, `delete`, `exec` im Namen | Kompiliert mit Read-Only-Sandbox für K8s-Deployments |

### Empfohlene Config für Kubernetes

```toml
# ~/.toolrecall/config.toml — K8s-optimiert
[security]
read_only_sandbox = true

[mcp]
allowed_paths = ["/data", "/projects"]
allow_terminal = false
allow_invalidate = false

[sources.memory]
enabled = false  # Hermes memory existiert nicht im Container
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

# Via HTTP
curl http://localhost:8567/health
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
  -p 8567:8567 \
  toolrecall:full

# With Ollama (for offline inference)
docker build --target with-ollama -t toolrecall:ollama .
docker run -d \
  -v toolrecall_data:/data \
  -v ollama_models:/root/.ollama \
  -p 8567:8567 \
  -p 11434:11434 \
  -e ENABLE_OLLAMA=true \
  -e OLLAMA_MODEL=llama3.2:1b \
  toolrecall:ollama
```