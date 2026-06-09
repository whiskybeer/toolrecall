# Sandbox Container Pool — Zero-Latency Isolation for Agent Tasks

## Problem

Docker `run --rm` costs **1-2 seconds** per task (pull, create, start). For
frequent operations (kubectl, npm install, pip in untrusted code), that latency
adds up fast.

## Solution: Warm Container Pool

Keep N containers running `sleep infinity`. When a task comes in, pick one from
the pool via `docker exec` (~5ms). Recreate it immediately so the pool stays
warm.

```
                                 ┌──────────────────┐
                                 │  Container Pool  │
                                 │  ┌──────────────┐│
   sandbox-exec "kubectl get po"──┤  Container 1    │ (in use)
                                 │  ┌──────────────┐│
                                 │  Container 2    │ (hot standby)
                                 │  ┌──────────────┐│
                                 │  Container 3    │ (hot standby)
                                 │  └──────────────┘│
                                 └──────────────────┘
```

## Configuration

### 1. Daemon Script

Place `sandbox-daemon.sh` in your agent's scripts directory:

```bash
# Start pool of 3
~/.hermes/scripts/sandbox-daemon.sh start 3

# Execute in pool
~/.hermes/scripts/sandbox-daemon.sh exec "kubectl get pods -A"

# Stop pool
~/.hermes/scripts/sandbox-daemon.sh stop
```

### 2. systemd (persistent pool)

```ini
# /etc/systemd/system/hermes-sandbox-pool.service
[Unit]
Description=Hermes Sandbox Container Pool
After=docker.service
Requires=docker.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=%h/.hermes/scripts/sandbox-daemon.sh start 3
ExecStop=%h/.hermes/scripts/sandbox-daemon.sh stop

[Install]
WantedBy=default.target
```

### 3. Kubernetes Integration

For kubectl-heavy workflows, mount the kubeconfig into the pool container:

```bash
# Create pool container with kubeconfig mount
docker create --rm \
  --network none --read-only \
  --tmpfs /tmp:size=100M \
  --memory="256m" --cpus="1" \
  --security-opt no-new-privileges \
  --cap-drop=ALL \
  -v ~/.kube:/home/.kube:ro \
  -e KUBECONFIG=/home/.kube/config \
  debian:bookworm-slim \
  sleep infinity
```

Then install kubectl in the image for a permanent pool:

```dockerfile
FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
    kubectl \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*
CMD ["sleep", "infinity"]
```

## Latency Comparison

| Mode | Cold (docker run) | Pool (docker exec) |
|------|------------------|-------------------|
| kubectl get pods | ~1.5s | ~15ms |
| pip install | ~5s + install | ~8ms + install |
| ls /tmp | ~1.2s | ~5ms |

## Security

Each container runs with:
- `--network none` — no internet access
- `--read-only` — no filesystem writes
- `--memory="256m" --cpus="1"` — resource caps
- `--cap-drop=ALL` — no kernel capabilities
- `--security-opt no-new-privileges` — no privilege escalation

The pool container itself has **zero** persistent state — recreate on every
replenish. This guarantees a clean isolation boundary per task.
