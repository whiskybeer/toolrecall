#!/usr/bin/env bash
# cold run + N identical warm reruns through the fixed stream cache. Isolated rig.
# Usage: repeat_replay.sh <n_warm_reruns>
set -u
REPO="$(cd "$(dirname "$0")/../.." && pwd)"; B="${BENCH_DIR:-/tmp/tr-cc-bench}"
RIG=$B/e6rig; OUT=$B/e6; PORT=8596; N=${1:-8}
[ -e "$RIG" ] && { echo "refusing to reuse $RIG"; exit 1; }
cleanup() {  # stop only processes whose environment points at THIS rig's DB
  for p in $(pgrep -f "run_forward_proxy|toolrecall daemon"); do
    tr '\0' '\n' < /proc/$p/environ 2>/dev/null | grep -qx "TOOLRECALL_CACHE_DB=$RIG/cache.db" && kill "$p"
  done
}
trap cleanup EXIT
mkdir -p "$RIG/home/.toolrecall" "$OUT"; SOCK="$(mktemp -d /tmp/trv6.XXXXXX)"
(
  export HOME="$RIG/home" PYTHONPATH="$REPO" TOOLRECALL_TRANSPORT="$SOCK/sock" \
    TOOLRECALL_CACHE_DB="$RIG/cache.db" TOOLRECALL_LOG_FILE="$RIG/toolrecall.log" \
    TOOLRECALL_NO_AUTOSTART=1 TOOLRECALL_UPDATE_CHECK=0 TOOLRECALL_API_TTL=86400 \
    TOOLRECALL_FORWARD_STREAM_TIMEOUT=600 TOOLRECALL_KEY_TRACE_DIR="$RIG/trace"
  python3 -m toolrecall daemon > "$RIG/daemon.log" 2>&1
  for _ in $(seq 100); do [ -S "$SOCK/sock" ] && break; sleep 0.3; done
  python3 -c "from toolrecall.proxy import run_forward_proxy; run_forward_proxy(bind='127.0.0.1', port=$PORT)" \
    > "$RIG/proxy.log" 2>&1 &
  sleep 2
)
[ -S "$SOCK/sock" ] || { echo "DAEMON DOWN"; cat "$RIG/daemon.log"; exit 1; }
PYTHONPATH=$REPO python3 -m toolrecall hook claude ci-env --port $PORT > "$OUT/ci.sh"
source "$OUT/ci.sh"
for i in $(seq 0 "$N"); do
  W=$B/e6work/task; rm -rf "$W"; mkdir -p "$W"
  cat > "$W/stats.py" <<'PY'
def mean(xs):
    return sum(xs) / len(xs) - 1


def median(xs):
    s = sorted(xs)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2
PY
  cat > "$W/test_stats.py" <<'PY'
from stats import mean, median


def test_mean():
    assert mean([1, 2, 3]) == 2


def test_median():
    assert median([3, 1, 2]) == 2
    assert median([4, 1, 3, 2]) == 2.5
PY
  ( cd "$W" && env -u CLAUDECODE -u CLAUDE_CODE_ENTRYPOINT -u CLAUDE_CODE_SESSION_ID \
      timeout 600 claude -p "The tests in test_stats.py fail. Find the bug in stats.py, fix it, and run python3 -m pytest -q to confirm. Reply with one line." \
      --model haiku "${TR_CLAUDE_FLAGS[@]}" --allowedTools "Read,Edit,Bash(python3 -m pytest*)" \
      --output-format json < /dev/null > "$OUT/run$i.json" 2> "$OUT/run$i.err" )
  ok=$(cd "$W" && python3 -m pytest -q 2>&1 | tail -1)
  echo "run$i $(python3 -c "import json;d=json.load(open('$OUT/run$i.json'));print('turns',d.get('num_turns'),'sid',d.get('session_id'))") | $ok"
done
PYTHONPATH=$REPO python3 -m toolrecall keytrace "$RIG/trace" > "$OUT/keytrace.txt"
