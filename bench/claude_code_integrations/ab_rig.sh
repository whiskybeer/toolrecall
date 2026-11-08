#!/usr/bin/env bash
# rig.sh start|stop — isolated ToolRecall daemon + forward proxy for arm B. Own DB/HOME/socket.
set -u
TR=${TR_REPO:-$(cd "$(dirname "$0")/../.." && pwd)}
RIG=${RIG:-/home/hermes/bench-data/ab-rig}; PORT=${PORT:-8595}
stop() {  # stop only processes whose environment points at THIS rig's DB
  for p in $(pgrep -f "run_forward_proxy|toolrecall daemon"); do
    tr '\0' '\n' < /proc/$p/environ 2>/dev/null | grep -qx "TOOLRECALL_CACHE_DB=$RIG/cache.db" && kill "$p" && echo "stopped $p"
  done
}
if [ "${1:-start}" = stop ]; then stop; exit 0; fi
[ -e "$RIG" ] && { echo "refusing to reuse $RIG"; exit 1; }
mkdir -p "$RIG/home/.toolrecall"; SOCK="$(mktemp -d /tmp/trvab.XXXXXX)"
export HOME="$RIG/home" PYTHONPATH="$TR" TOOLRECALL_TRANSPORT="$SOCK/sock" \
  TOOLRECALL_CACHE_DB="$RIG/cache.db" TOOLRECALL_LOG_FILE="$RIG/toolrecall.log" \
  TOOLRECALL_NO_AUTOSTART=1 TOOLRECALL_UPDATE_CHECK=0 TOOLRECALL_API_TTL=604800 \
  TOOLRECALL_FORWARD_STREAM_TIMEOUT=900 TOOLRECALL_KEY_TRACE_DIR="$RIG/trace"
python3 -m toolrecall daemon > "$RIG/daemon.log" 2>&1
for _ in $(seq 100); do [ -S "$SOCK/sock" ] && break; sleep 0.3; done
[ -S "$SOCK/sock" ] || { echo "DAEMON DOWN"; cat "$RIG/daemon.log"; stop; exit 1; }
nohup python3 -c "from toolrecall.proxy import run_forward_proxy; run_forward_proxy(bind='127.0.0.1', port=$PORT)" \
  > "$RIG/proxy.log" 2>&1 &
sleep 2
ss -ltn | grep -q ":$PORT " && echo "rig up: proxy :$PORT, daemon $SOCK/sock" || { echo "PROXY DOWN"; stop; exit 1; }
