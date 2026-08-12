#!/usr/bin/env bash
# run_accumulate.sh — Accumulating-loop A/B (Phase 1+2): 10 instances × 8 turns.
# Turn N = N requests (real agent loop). Tests savings curve + prefix-caching claim.
#
# Requires: OPENROUTER_API_KEY (reused for both arms per user)
#   MEASURE_INSTANCES / MEASURE_MAX_TURNS overridable
set -euo pipefail

KEY="${OPENROUTER_API_KEY:?set OPENROUTER_API_KEY}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MEASURE="$SCRIPT_DIR/measure_swebench.py"
PORT=4000

export MEASURE_INSTANCES="${MEASURE_INSTANCES:-10}"
export MEASURE_MAX_TURNS="${MEASURE_MAX_TURNS:-8}"
export MEASURE_MODEL="${MEASURE_MODEL:-openrouter/deepseek/deepseek-v4-flash}"
export MEASURE_PRICE="${MEASURE_PRICE:-0.15}"

echo "=== LiteLLM Dedup — ACCUMULATING agent loop ==="
echo "  Instances:  $MEASURE_INSTANCES"
echo "  Turns/inst: $MEASURE_MAX_TURNS  → $((MEASURE_INSTANCES*MEASURE_MAX_TURNS)) requests/arm"
echo "  Model:      $MEASURE_MODEL"

write_proxy() {  # $1=path $2=key $3=disabled?
cat > "$1" << PROXYEOF
model_list:
  - model_name: deepseek
    litellm_params:
      model: ${MEASURE_MODEL}
      api_key: ${2}
litellm_settings:
  callbacks: toolrecall.adapters.litellm.handler
general_settings:
  master_key: sk-bench
PROXYEOF
}

start_proxy() {  # $1=config $2=log $3=disabled?
  pkill -f "litellm --config /tmp/proxy_" 2>/dev/null || true
  sleep 1
  if [ "$3" = "1" ]; then
    TOOLRECALL_DEDUP_DISABLED=1 TOOLRECALL_SHIM_DISABLE=1 litellm --config "$1" --port $PORT > "$2" 2>&1 &
  else
    TOOLRECALL_SHIM_DISABLE=1 litellm --config "$1" --port $PORT > "$2" 2>&1 &
  fi
  for i in $(seq 1 30); do
    curl -sf -H "Authorization: Bearer sk-bench" http://localhost:$PORT/health >/dev/null 2>&1 && return 0
    sleep 1
  done
  echo "  ❌ proxy failed"; tail -20 "$2"; return 1
}

echo ""
echo "==> ARM 1/2: WITH dedup (accumulating)"
write_proxy /tmp/proxy_accum_with.yaml "$KEY" 0
start_proxy /tmp/proxy_accum_with.yaml /tmp/proxy_accum_with.log 0
cd /home/hermes/toolrecall
TOOLRECALL_SHIM_DISABLE=1 python3 "$MEASURE" --accumulate --json > /tmp/swe_accum_with.json
pkill -f "litellm --config /tmp/proxy_" 2>/dev/null || true
sleep 1

echo ""
echo "==> ARM 2/2: WITHOUT dedup (accumulating)"
write_proxy /tmp/proxy_accum_without.yaml "$KEY" 1
start_proxy /tmp/proxy_accum_without.yaml /tmp/proxy_accum_without.log 1
TOOLRECALL_DEDUP_DISABLED=1 TOOLRECALL_SHIM_DISABLE=1 python3 "$MEASURE" --accumulate --disabled --json > /tmp/swe_accum_without.json
pkill -f "litellm --config /tmp/proxy_" 2>/dev/null || true

echo ""
echo "=============================================="
echo "  COMPARISON — ACCUMULATING LOOP"
echo "=============================================="
python3 "$MEASURE" --compare /tmp/swe_accum_with.json /tmp/swe_accum_without.json
