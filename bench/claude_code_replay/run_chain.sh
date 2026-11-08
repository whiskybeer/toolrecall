#!/usr/bin/env bash
# run_chain.sh REP → results/r<REP>_s<k>.*  One Claude Code session works through five
# django/db/migrations issues (claude -p, then --resume), through the replay proxy on :8590
# with the claude-code profile. FIXED work path (cwd is part of the prompt).
# CHAIN_EXTRA_ENV="PYTHONHASHSEED=0" makes the agent's Python tool runs deterministic (section 2e).
set -uo pipefail
L="${CC_CHAIN_DIR:?set CC_CHAIN_DIR (inst/, env/dj, swe-repos/django, grade_dj.py)}"; REPO=$L/swe-repos/django
ISSUES=(django__django-15738 django__django-15996 django__django-16595 django__django-16820 django__django-17087)
REP=$1; OUT=$L/results; mkdir -p $OUT
W=$L/work/chain
git -C $REPO worktree remove --force $W 2>/dev/null; rm -rf $W
git -C $REPO worktree add -f --detach $W $(cat $L/inst/${ISSUES[0]}/base_commit) >/dev/null 2>&1
ARGS=(--model sonnet --output-format json --dangerously-skip-permissions --strict-mcp-config --mcp-config '{"mcpServers":{}}')
SID=""; k=0
for ID in "${ISSUES[@]}"; do
  I=$L/inst/$ID; St=r${REP}_s$k
  git -C $W checkout -f -q $(cat $I/base_commit) && git -C $W clean -fdq
  if [ -z "$SID" ]; then
    INTRO="You are working in a checkout of the django/django repository, going through a backlog of django/db/migrations bugs one at a time in this session."
  else
    INTRO="Next issue from the backlog. The checkout has been reset to the commit this issue was filed against, so your earlier changes are gone."
  fi
  PROMPT="$INTRO Fix the GitHub issue below by changing the library source code. Do not modify or add tests under tests/ — hidden tests will grade your fix.

Work like a careful engineer: first find and run the existing test module(s) closest to the code you are changing, then after every edit re-run those modules and read the full output before continuing. Run tests with
  cd tests && PYTHONPATH=.. $L/env/dj/bin/python runtests.py --parallel 1 <module labels, e.g. migrations.test_writer>

When the fix is complete and the relevant modules pass, stop and give a one-paragraph summary.

<issue>
$(cat $I/problem_statement)
</issue>"
  RES=(); [ -n "$SID" ] && RES=(--resume "$SID")
  t0=$(date +%s)
  (cd $W && env -u CLAUDECODE -u CLAUDE_CODE_SESSION_ID -u CLAUDE_CODE_ENTRYPOINT \
     ${CHAIN_EXTRA_ENV:-} ANTHROPIC_BASE_URL=http://127.0.0.1:8590 ANTHROPIC_CUSTOM_HEADERS="X-ToolRecall-Canonicalize: claude-code" \
     timeout 1800 claude -p "$PROMPT" "${RES[@]}" "${ARGS[@]}" < /dev/null > $OUT/$St.json 2> $OUT/$St.err)
  echo "rc=$? wall=$(( $(date +%s) - t0 ))" > $OUT/$St.meta
  [ -z "$SID" ] && SID=$(python3 -c "import json;print(json.load(open('$OUT/$St.json')).get('session_id',''))" 2>/dev/null)
  echo "$SID" > $OUT/r${REP}.sid
  git -C $W add -A -N 2>/dev/null; git -C $W diff -- . ':(exclude)tests' > $OUT/$St.diff
  G=$L/work/grade; git -C $REPO worktree remove --force $G 2>/dev/null; rm -rf $G
  git -C $REPO worktree add -f --detach $G $(cat $I/base_commit) >/dev/null 2>&1
  git -C $G apply --whitespace=nowarn $OUT/$St.diff 2>/dev/null || echo "diff-apply-fail" > $OUT/$St.grade
  $L/env/dj/bin/python $L/grade_dj.py $ID $G >> $OUT/$St.grade
  git -C $REPO worktree remove --force $G
  echo "$St $ID $(cat $OUT/$St.meta) $(tr '\n' ' ' < $OUT/$St.grade)"
  k=$((k+1))
done
git -C $REPO worktree remove --force $W
echo "CHAIN DONE r$REP session=$SID"
