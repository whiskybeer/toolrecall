#!/bin/bash
# Prefix-cache share test: same repo, DIFFERENT tasks, through Warp's real harness.
# Run AFTER Robin does 2 Warp sessions (A: task1, B: task2, same repo).
# Usage: bash prefix_test.sh <session-A-start-timestamp> <session-B-start-timestamp>
# CSV path is overridable via TOOLRECALL_PROXY_CSV (default: ~/.toolrecall).
set -u
CSV="${TOOLRECALL_PROXY_CSV:-$HOME/.toolrecall/proxy_usage.csv}"
A_START=${1:-0}
B_START=${2:-0}

python3 - "$CSV" "$A_START" "$B_START" <<'PYEOF'
import sys, csv

csv_path, a_start, b_start = sys.argv[1], float(sys.argv[2]), float(sys.argv[3])
rows = list(csv.DictReader(open(csv_path)))
rows = [r for r in rows if float(r['timestamp']) >= a_start]

def bucket(r):
    ts = float(r['timestamp'])
    if ts >= b_start:
        return 'B'
    if ts >= a_start:
        return 'A'
    return None

stats = {'A': [0,0,0], 'B': [0,0,0]}  # [reqs, prompt_tok, cached_tok]
for r in rows:
    b = bucket(r)
    if not b: continue
    stats[b][0] += 1
    stats[b][1] += int(r['prompt_tokens'] or 0)
    stats[b][2] += int(r['cache_read_tokens'] or 0)

print(f"{'session':>8} {'reqs':>5} {'prompt_tok':>12} {'prefix-cached':>14} {'share':>7}")
for b in ('A', 'B'):
    n, pt, ct = stats[b]
    share = f"{100*ct/pt:.0f}%" if pt else "n/a"
    print(f"{b:>8} {n:>5} {pt:>12,} {ct:>14,} {share:>7}")
print()
print("Interpretation: session B (different task, same repo) — the prefix-cached")
print("share shows how much of the shared repo/system context the PROVIDER already")
print("absorbed automatically. ToolRecall's api_cache can only add exact replays")
print("(0 here if the tasks differed); the file cache needs the local MCP path.")
PYEOF
