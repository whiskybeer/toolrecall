#!/usr/bin/env bash
# rig.sh — hermetic daemon + forward proxy for the Claude Code replay bench.
# Own socket, DB and HOME (the proxy's usage CSV lands in $RIG_DIR/home), so the
# live instance is never touched.   Usage: RIG_DIR=/tmp/ccr-s1 bash rig.sh [start|stop]
set -euo pipefail
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
RIG_DIR="${RIG_DIR:?set RIG_DIR (one per run)}"; PORT="${BENCH_PROXY_PORT:-8590}"
if [ "${1:-start}" = stop ]; then
  for f in proxy daemon; do [ -f "$RIG_DIR/$f.pid" ] && kill "$(cat "$RIG_DIR/$f.pid")" 2>/dev/null || true; done
  pid=$(sed -n 's/.*Daemon started (PID: \([0-9]*\)).*/\1/p' "$RIG_DIR/daemon.log" 2>/dev/null | tail -1)
  [ -n "$pid" ] && kill "$pid" 2>/dev/null || true; echo stopped; exit 0
fi
[ -e "$RIG_DIR" ] && { echo "refusing to reuse $RIG_DIR" >&2; exit 1; }
mkdir -p "$RIG_DIR/home"; SOCK="$(mktemp -d /tmp/ccr.XXXXXX)"
export HOME="$RIG_DIR/home" PYTHONPATH="$REPO" TOOLRECALL_TRANSPORT="$SOCK/sock" \
  TOOLRECALL_CACHE_DB="$RIG_DIR/cache.db" TOOLRECALL_LOG_FILE="$RIG_DIR/toolrecall.log" \
  TOOLRECALL_NO_AUTOSTART=1 TOOLRECALL_UPDATE_CHECK=0 TOOLRECALL_API_TTL=86400 \
  TOOLRECALL_FORWARD_STREAM_TIMEOUT=600
python3 -m toolrecall daemon > "$RIG_DIR/daemon.log" 2>&1 & echo $! > "$RIG_DIR/daemon.pid"
for _ in $(seq 100); do [ -S "$SOCK/sock" ] && break; sleep 0.3; done
python3 -c "from toolrecall.proxy import run_forward_proxy; run_forward_proxy(bind='127.0.0.1', port=$PORT)" \
  > "$RIG_DIR/proxy.log" 2>&1 & echo $! > "$RIG_DIR/proxy.pid"
sleep 2; echo "proxy http://127.0.0.1:$PORT  usage CSV: $RIG_DIR/home/.toolrecall/proxy_usage.csv"
