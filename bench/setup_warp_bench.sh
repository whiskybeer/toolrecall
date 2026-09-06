#!/usr/bin/env bash
# setup_warp_bench.sh — bring up the full benchmark chain:
#   TR proxy (bench port, TOOLRECALL_API_TTL) + Warp edge on loopback, no auth.
# The arms are isolated by per-arm OpenRouter keys, not by edge auth.
#
# Usage: bash bench/setup_warp_bench.sh
#   Env: TOOLRECALL_API_TTL (default 3600), BENCH_PROXY_PORT (8572), BENCH_EDGE_PORT (8573)
# Prints READY when up. Logs to /tmp/warp_bench_proxy.log and /tmp/warp_bench_edge.log.
# The standalone proxy requires the daemon's own proxy NOT to conflict — the
# daemon keeps 8569; this rig uses 8572/8573.
set -euo pipefail
cd "$(dirname "$0")/.."

TTL="${TOOLRECALL_API_TTL:-3600}"
PROXY_PORT="${BENCH_PROXY_PORT:-8572}"
EDGE_PORT="${BENCH_EDGE_PORT:-8573}"

# Dedicated standalone proxy instance: bench port, bench TTL.
# run_forward_proxy binds and serves; TOOLRECALL_FORWARD_PORT sets the port.
TOOLRECALL_API_TTL="$TTL" \
TOOLRECALL_FORWARD_PORT="$PROXY_PORT" \
python3 -c "
import os
from toolrecall.proxy import run_forward_proxy
run_forward_proxy(bind='127.0.0.1', port=int(os.environ['TOOLRECALL_FORWARD_PORT']))
" > /tmp/warp_bench_proxy.log 2>&1 &
PROXY_PID=$!
sleep 2

TOOLRECALL_PROXY_PORT="$PROXY_PORT" \
python3 -m toolrecall.adapters.warp \
  --provider openrouter.ai --bind 127.0.0.1 --port "$EDGE_PORT" \
  > /tmp/warp_bench_edge.log 2>&1 &
EDGE_PID=$!
sleep 2

echo "PROXY_PID=$PROXY_PID EDGE_PID=$EDGE_PID PROXY_PORT=$PROXY_PORT EDGE_PORT=$EDGE_PORT TTL=$TTL"
curl -s -o /dev/null -w "edge health: HTTP %{http_code}\n" \
  "http://127.0.0.1:$EDGE_PORT/v1/models" || true
echo "READY"
