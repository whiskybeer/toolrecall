#!/usr/bin/env bash
# gepa_real_rig.sh — hermetic ToolRecall chain for bench/run_gepa_real.py:
#   own daemon (own socket + DB) → standalone forward proxy → Warp edge.
#
# Hermetic means: nothing touches the live instance. The proxy is a relay —
# its cache checks go to the daemon over TOOLRECALL_TRANSPORT — so the rig
# needs its own daemon, not just its own DB (GEPA_FORK_AB_RESULTS.md, "Rig
# facts"). HOME is pinned to the rig dir as well: the proxy appends usage
# rows to ~/.toolrecall/proxy_usage.csv, and the prod CSV is billed truth.
#
# Usage:  bash bench/gepa_real_rig.sh [start|stop]
#   Env:  RIG_DIR (default /tmp/gepa_real_rig — one per run, never reused;
#         start refuses an existing dir), BENCH_PROXY_PORT (8582),
#         BENCH_EDGE_PORT (8583)
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
RIG_DIR="${RIG_DIR:-/tmp/gepa_real_rig}"
PROXY_PORT="${BENCH_PROXY_PORT:-8582}"
EDGE_PORT="${BENCH_EDGE_PORT:-8583}"

if [ "${1:-start}" = "stop" ]; then
  for f in edge proxy daemon; do
    [ -f "$RIG_DIR/$f.pid" ] && kill "$(cat "$RIG_DIR/$f.pid")" 2>/dev/null || true
  done
  # the daemon re-execs: the PID it logs is the one serving the socket
  pid=$(sed -n 's/.*Daemon started (PID: \([0-9]*\)).*/\1/p' "$RIG_DIR/daemon.log" 2>/dev/null | tail -1)
  [ -n "$pid" ] && kill "$pid" 2>/dev/null || true
  [ -f "$RIG_DIR/sock_dir" ] && rm -rf "$(cat "$RIG_DIR/sock_dir")"
  echo "stopped"
  exit 0
fi

if [ -e "$RIG_DIR" ]; then
  echo "refusing to reuse $RIG_DIR (one rig dir per run)" >&2
  exit 1
fi
mkdir -p "$RIG_DIR/home"

export HOME="$RIG_DIR/home"
export PYTHONPATH="$REPO"
# AF_UNIX paths cap at ~107 bytes: a deep RIG_DIR silently leaves the daemon
# without a socket, so the socket gets its own short private dir.
SOCK_DIR="$(mktemp -d /tmp/gepa-rig.XXXXXX)"
echo "$SOCK_DIR" > "$RIG_DIR/sock_dir"
export TOOLRECALL_TRANSPORT="$SOCK_DIR/sock"
export TOOLRECALL_CACHE_DB="$RIG_DIR/cache.db"
export TOOLRECALL_LOG_FILE="$RIG_DIR/toolrecall.log"
export TOOLRECALL_NO_AUTOSTART=1
export TOOLRECALL_UPDATE_CHECK=0
export TOOLRECALL_API_TTL="${TOOLRECALL_API_TTL:-86400}"

python3 -m toolrecall daemon > "$RIG_DIR/daemon.log" 2>&1 &
echo $! > "$RIG_DIR/daemon.pid"
for _ in $(seq 300); do [ -S "$TOOLRECALL_TRANSPORT" ] && break; sleep 0.2; done
[ -S "$TOOLRECALL_TRANSPORT" ] || { echo "daemon socket never appeared" >&2; exit 1; }

TOOLRECALL_FORWARD_PORT="$PROXY_PORT" python3 -c "
import os
from toolrecall.proxy import run_forward_proxy
run_forward_proxy(bind='127.0.0.1', port=int(os.environ['TOOLRECALL_FORWARD_PORT']))
" > "$RIG_DIR/proxy.log" 2>&1 &
echo $! > "$RIG_DIR/proxy.pid"
sleep 2

TOOLRECALL_PROXY_PORT="$PROXY_PORT" python3 -m toolrecall.adapters.warp \
  --provider openrouter.ai --bind 127.0.0.1 --port "$EDGE_PORT" \
  > "$RIG_DIR/edge.log" 2>&1 &
echo $! > "$RIG_DIR/edge.pid"
sleep 2

echo "RIG_DIR=$RIG_DIR proxy=:$PROXY_PORT edge=:$EDGE_PORT"
echo "edge: http://127.0.0.1:$EDGE_PORT/v1/chat/completions"
echo "READY"
