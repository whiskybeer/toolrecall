#!/usr/bin/env bash
# run_once.sh <arm: direct|proxy> <label>  — recreate fixture at a FIXED path, run claude -p once
set -u
ARM=$1; LABEL=$2; OUT=/tmp/trhook/results; mkdir -p $OUT
W=/tmp/trhook/ccwork/task; rm -rf $W; mkdir -p $W
cat > $W/stats.py <<'PY'
def mean(xs):
    return sum(xs) / len(xs) - 1


def median(xs):
    s = sorted(xs)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2
PY
cat > $W/test_stats.py <<'PY'
from stats import mean, median


def test_mean():
    assert mean([1, 2, 3]) == 2


def test_median():
    assert median([3, 1, 2]) == 2
    assert median([4, 1, 3, 2]) == 2.5
PY
cd $W
if [ "$ARM" = proxy ]; then
  export ANTHROPIC_BASE_URL=http://127.0.0.1:8590
  export ANTHROPIC_CUSTOM_HEADERS="X-ToolRecall-Canonicalize: claude-code"
fi
timeout 600 claude -p "The tests in test_stats.py fail. Find the bug in stats.py, fix it, and run python3 -m pytest -q to confirm. Reply with one line." \
  --model haiku --strict-mcp-config --mcp-config '{"mcpServers":{}}' --allowedTools "Read,Edit,Bash(python3 -m pytest*)" --output-format json < /dev/null > $OUT/$LABEL.json 2> $OUT/$LABEL.err
echo "rc=$? $(python3 -c "import json;d=json.load(open('$OUT/$LABEL.json'));print('turns',d.get('num_turns'),'cost',d.get('total_cost_usd'),'|',(d.get('result') or '')[:80])" 2>&1)"
python3 -m pytest -q 2>&1 | tail -1
