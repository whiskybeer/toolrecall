#!/usr/bin/env bash
# rig.sh start|stop|status — isolated ToolRecall daemon + forward proxy for Arm B.
# Own HOME, socket, SQLite DB, and usage CSV. Never touches live ~/.toolrecall/.
set -u

TR=${TR_REPO:-$(cd "$(dirname "$0")/../.." && pwd)}
RIG=${RIG:-$HOME/bench-data/crossover-rig}
PORT=${PORT:-8595}
DEDUP=${TR_DEDUP:-1}
DEDUP_MIN_CHARS=${TOOLRECALL_DEDUP_MIN_CHARS:-800}
DEDUP_PROTECT_LAST=${TOOLRECALL_DEDUP_PROTECT_LAST:-2}

stop() {
  for p in $(pgrep -f "run_forward_proxy|toolrecall daemon" 2>/dev/null || true); do
    if tr '\0' '\n' < "/proc/$p/environ" 2>/dev/null | grep -qx "TOOLRECALL_CACHE_DB=$RIG/cache.db"; then
      kill "$p" && echo "stopped $p"
    fi
  done
}

status() {
  if ss -ltn | grep -q ":$PORT "; then
    echo "rig up on :$PORT (RIG=$RIG)"
    return 0
  fi
  echo "rig down on :$PORT (RIG=$RIG)"
  return 1
}

case "${1:-start}" in
  stop)
    stop
    exit 0
    ;;
  status)
    status
    exit $?
    ;;
  start)
    ;;
  *)
    echo "Usage: $0 {start|stop|status}" >&2
    exit 2
    ;;
esac

if [ -e "$RIG" ]; then
  echo "refusing to reuse existing $RIG (remove or set RIG=... to a fresh path)" >&2
  exit 1
fi

mkdir -p "$RIG/home/.toolrecall"
SOCK="$(mktemp -d /tmp/tr-xover.XXXXXX)"

if [ "$DEDUP" = "1" ] || [ "$DEDUP" = "true" ]; then
  DEDUP_TOML="true"
else
  DEDUP_TOML="false"
fi

cat > "$RIG/home/.toolrecall/config.toml" <<EOF
[forward_proxy]
dedup = $DEDUP_TOML
EOF

export HOME="$RIG/home"
export PYTHONPATH="$TR"
export TOOLRECALL_TRANSPORT="$SOCK/sock"
export TOOLRECALL_CACHE_DB="$RIG/cache.db"
export TOOLRECALL_LOG_FILE="$RIG/toolrecall.log"
export TOOLRECALL_USAGE_LOG="$RIG/proxy_usage.csv"
export TOOLRECALL_NO_AUTOSTART=1
export TOOLRECALL_UPDATE_CHECK=0
export TOOLRECALL_API_TTL=604800
export TOOLRECALL_FORWARD_STREAM_TIMEOUT=900
export TOOLRECALL_KEY_TRACE_DIR="$RIG/trace"
export TOOLRECALL_DEDUP_MIN_CHARS="$DEDUP_MIN_CHARS"
export TOOLRECALL_DEDUP_PROTECT_LAST="$DEDUP_PROTECT_LAST"

python3 -m toolrecall daemon > "$RIG/daemon.log" 2>&1
for _ in $(seq 100); do
  [ -S "$SOCK/sock" ] && break
  sleep 0.3
done
if [ ! -S "$SOCK/sock" ]; then
  echo "DAEMON DOWN" >&2
  cat "$RIG/daemon.log" >&2
  stop
  exit 1
fi

nohup python3 -c "from toolrecall.proxy import run_forward_proxy; run_forward_proxy(bind='127.0.0.1', port=$PORT)" \
  > "$RIG/proxy.log" 2>&1 &
sleep 2

if ss -ltn | grep -q ":$PORT "; then
  echo "rig up: proxy :$PORT, daemon $SOCK/sock, dedup=$DEDUP_TOML, usage=$RIG/proxy_usage.csv"
else
  echo "PROXY DOWN" >&2
  cat "$RIG/proxy.log" >&2
  stop
  exit 1
fi
