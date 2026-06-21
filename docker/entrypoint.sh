#!/bin/bash
set -e

# ToolRecall + Ollama Entrypoint
# Starts daemon, optionally proxy, optionally ollama
# In with-ollama image: runs as root, drops to toolrecall for daemon

DATA_DIR="${TOOLRECALL_DATA:-/data}"
mkdir -p "$DATA_DIR"

export TOOLRECALL_CACHE_DB="${DATA_DIR}/cache.db"
export TOOLRECALL_KNOWLEDGE_DB="${DATA_DIR}/knowledge.db"
export TOOLRECALL_UDS_PATH="${DATA_DIR}/tc.sock"

# Determine run user: toolrecall (non-root) if exists, else current
if id toolrecall &>/dev/null; then
    RUN_USER="toolrecall"
    chown toolrecall:toolrecall "$DATA_DIR" 2>/dev/null || true
else
    RUN_USER=""
fi

_run() {
    if [ -n "$RUN_USER" ]; then
        su -s /bin/bash "$RUN_USER" -c "$*"
    else
        eval "$*"
    fi
}

# Start daemon
echo "[toolrecall] Starting daemon..."
_run "toolrecall daemon --foreground" &
DAEMON_PID=$!

# Wait for socket
for i in $(seq 1 10); do
    if [ -S "$TOOLRECALL_UDS_PATH" ]; then
        echo "[toolrecall] Daemon ready (socket: $TOOLRECALL_UDS_PATH)"
        break
    fi
    sleep 1
done

# Optionally index memory (as non-root user)
if [ "${INDEX_MEMORY:-true}" = "true" ]; then
    echo "[toolrecall] Indexing Hermes memory..."
    _run "toolrecall index-memory" 2>/dev/null || true
fi

# Optionally start HTTP proxy
if [ "${ENABLE_PROXY:-true}" = "true" ]; then
    echo "[toolrecall] Starting HTTP proxy on port 8569..."
    _run "toolrecall serve" &
fi

# Optionally start Ollama (runs as root — needs device access)
if [ "${ENABLE_OLLAMA:-false}" = "true" ]; then
    echo "[ollama] Starting Ollama on port 11434..."
    ollama serve &
    OLLAMA_PID=$!
fi

echo "[toolrecall] Ready."

# Wait for all background processes
trap "kill $DAEMON_PID ${OLLAMA_PID:-} 2>/dev/null; exit 0" INT TERM
wait