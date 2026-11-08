#!/usr/bin/env bash
# live Claude Code through an isolated ToolRecall proxy, dedup+normalizer ON vs per-request opt-out.
# Usage: dedup_live.sh <last_rep> [first_rep]   (rep 1 = cache warm-up pair)
set -u
REPO="$(cd "$(dirname "$0")/../.." && pwd)"; B="${BENCH_DIR:-/tmp/tr-cc-bench}"
RIG=$B/e3rig; OUT=$B/e3; PORT=8597
REPS=${1:-3}
mkdir -p "$OUT"
cleanup() {  # stop only processes whose environment points at THIS rig's DB
  for p in $(pgrep -f "run_forward_proxy|toolrecall daemon"); do
    tr '\0' '\n' < /proc/$p/environ 2>/dev/null | grep -qx "TOOLRECALL_CACHE_DB=$RIG/cache.db" && kill "$p"
  done
}
trap cleanup EXIT
if [ ! -e "$RIG/proxy.pid" ]; then
  mkdir -p "$RIG/home/.toolrecall"; SOCK="$(mktemp -d /tmp/trv.XXXXXX)"
  printf '[forward_proxy]\ndedup = true\n' > "$RIG/config.toml"
  (
    export HOME="$RIG/home" PYTHONPATH="$REPO" TOOLRECALL_TRANSPORT="$SOCK/sock" \
      TOOLRECALL_CACHE_DB="$RIG/cache.db" TOOLRECALL_LOG_FILE="$RIG/toolrecall.log" \
      TOOLRECALL_CONFIG="$RIG/config.toml" TOOLRECALL_DEDUP_NORMALIZE=1 \
      TOOLRECALL_NO_AUTOSTART=1 TOOLRECALL_UPDATE_CHECK=0 TOOLRECALL_FORWARD_STREAM_TIMEOUT=600
    python3 -m toolrecall daemon > "$RIG/daemon.log" 2>&1 & echo $! > "$RIG/daemon.pid"
    for _ in $(seq 100); do [ -S "$SOCK/sock" ] && break; sleep 0.3; done
    python3 -c "from toolrecall.proxy import run_forward_proxy; run_forward_proxy(bind='127.0.0.1', port=$PORT)" \
      > "$RIG/proxy.log" 2>&1 & echo $! > "$RIG/proxy.pid"
    sleep 2
  )
fi

fixture() {
  W=$1; rm -rf "$W"; mkdir -p "$W"
  cat > "$W/inventory.py" <<'PY'
def restock(items, threshold):
    return [dict(i, qty=i["qty"] + 10) for i in items if i["qty"] < threshold]
PY
  cat > "$W/test_inventory.py" <<'PY'
from inventory import restock

ITEMS = [{"sku": f"SKU-{n:04d}", "name": f"widget number {n}", "qty": n % 7} for n in range(40)]


def test_restock_keeps_all_items():
    out = restock(ITEMS, threshold=3)
    assert out == [dict(i, qty=i["qty"] + 10) if i["qty"] < 3 else i for i in ITEMS]
PY
}

PROMPT='We suspect test_inventory.py is flaky. Do NOT change any file. Run `python3 -m pytest test_inventory.py` exactly three times, as three separate Bash calls, one after another. Then answer in one sentence: did all three runs give the same result?'

for i in $(seq "${2:-1}" "$REPS"); do
  if [ $((i % 2)) -eq 1 ]; then ARMS="nodedup dedup"; else ARMS="dedup nodedup"; fi
  for ARM in $ARMS; do
    W=$B/e3work/task; fixture "$W"
    HDR=""; [ "$ARM" = nodedup ] && HDR="X-ToolRecall-No-Dedup: 1"
    ( cd "$W" && env -u CLAUDECODE -u CLAUDE_CODE_ENTRYPOINT -u CLAUDE_CODE_SESSION_ID \
        ANTHROPIC_BASE_URL=http://127.0.0.1:$PORT ANTHROPIC_CUSTOM_HEADERS="$HDR" \
        timeout 600 claude -p "$PROMPT" --model haiku \
        --strict-mcp-config --mcp-config '{"mcpServers":{}}' \
        --allowedTools "Bash(python3 -m pytest*)" --output-format json < /dev/null \
        > "$OUT/$ARM-$i.json" 2> "$OUT/$ARM-$i.err" )
    python3 -c "import json;d=json.load(open('$OUT/$ARM-$i.json'));u=d['usage'];print('$ARM',$i,'cost',d['total_cost_usd'],'turns',d['num_turns'],'in',u['input_tokens'],'cw',u['cache_creation_input_tokens'],'cr',u['cache_read_input_tokens'],'out',u['output_tokens'],'|',d['result'][:90])"
  done
done
grep -c "dedup" "$RIG/proxy.log" || true
