#!/usr/bin/env bash
# ctl_run.sh ID ARM LABEL — one single-issue Sonnet run for the claude-code profile controls.
#   ARM: replay (profile header) | fresh (X-ToolRecall-No-Cache) | cwdB (profile, other checkout dir)
#        | edited (profile, one comment line added in serializer.py) | haiku (profile, other model)
#        | edhidden (edited, hidden from git status) | commit (profile, one extra empty commit on HEAD)
# Records proxy CSV rows written during this run → results/LABEL.csv
set -uo pipefail
L="${CC_CHAIN_DIR:?set CC_CHAIN_DIR (inst/, env/dj, swe-repos/django, grade_dj.py)}"; C="${CC_CTL_DIR:-/tmp/cc-ctl}"; REPO=$L/swe-repos/django; ID=$1; ARM=$2; LABEL=$3
I=$L/inst/$ID; OUT=$C/results; mkdir -p $OUT; CSV=$C/rig/home/.toolrecall/proxy_usage.csv
W=$C/wA; [ $ARM = cwdB ] && W=$C/wB
git -C $REPO worktree remove --force $W 2>/dev/null; rm -rf $W
git -C $REPO worktree add -f --detach $W $(cat $I/base_commit) >/dev/null 2>&1
if [ $ARM = commit ]; then
  # one empty commit with fixed identity + dates → identical hash every run (isolates "new HEAD")
  GIT_AUTHOR_DATE="2023-01-01T00:00:00Z" GIT_COMMITTER_DATE="2023-01-01T00:00:00Z" \
    git -C $W -c user.name=ci -c user.email=ci@example.invalid commit -q --allow-empty -m "ci: unrelated change"
fi
if [ $ARM = edited ] || [ $ARM = edhidden ]; then
  python3 - $W/django/db/migrations/serializer.py <<'PY'
import sys; p=sys.argv[1]; s=open(p).read()
for anchor in ("class EnumSerializer(BaseSerializer):\n", "class FunctionTypeSerializer(BaseSerializer):\n"):
    assert anchor in s, anchor
    s=s.replace(anchor, anchor+"    # NOTE(control): local edit, not part of upstream\n",1)
open(p,"w").write(s)
PY
  # edhidden: hide the edit from `git status` (Claude Code puts git status in its system prompt),
  # so the edit is only visible through file content the agent reads.
  [ $ARM = edhidden ] && git -C $W update-index --assume-unchanged django/db/migrations/serializer.py
fi
PROMPT="You are working in a checkout of the django/django repository. Fix the GitHub issue below by changing the library source code. Do not modify or add tests under tests/ — hidden tests will grade your fix.

Work like a careful engineer: first find and run the existing test module(s) closest to the code you are changing, then after every edit re-run those modules and read the full output before continuing. Run tests with
  cd tests && PYTHONPATH=.. $L/env/dj/bin/python runtests.py --parallel 1 <module labels, e.g. migrations.test_writer>

When the fix is complete and the relevant modules pass, stop and give a one-paragraph summary.

<issue>
$(cat $I/problem_statement)
</issue>"
HDR="X-ToolRecall-Canonicalize: claude-code"; [ $ARM = fresh ] && HDR="X-ToolRecall-No-Cache: 1"
MODEL=sonnet; [ $ARM = haiku ] && MODEL=haiku
n0=$(wc -l < $CSV); t0=$(date +%s)
(cd $W && env -u CLAUDECODE -u CLAUDE_CODE_SESSION_ID -u CLAUDE_CODE_ENTRYPOINT \
   ANTHROPIC_BASE_URL=http://127.0.0.1:8590 ANTHROPIC_CUSTOM_HEADERS="$HDR" \
   nice -n 10 timeout 1800 claude -p "$PROMPT" --model $MODEL --output-format json --dangerously-skip-permissions \
   --strict-mcp-config --mcp-config '{"mcpServers":{}}' < /dev/null > $OUT/$LABEL.json 2> $OUT/$LABEL.err)
echo "rc=$? wall=$(( $(date +%s) - t0 )) cwd=$W" > $OUT/$LABEL.meta
sleep 1; tail -n +$((n0+1)) $CSV > $OUT/$LABEL.csv
# grade the agent's diff (excluding tests/ and the control edit, which lives only in serializer.py comments)
git -C $W update-index --no-assume-unchanged django/db/migrations/serializer.py 2>/dev/null
git -C $W add -A -N 2>/dev/null; git -C $W diff -- . ':(exclude)tests' > $OUT/$LABEL.diff
G=$C/grade; git -C $REPO worktree remove --force $G 2>/dev/null; rm -rf $G
git -C $REPO worktree add -f --detach $G $(cat $I/base_commit) >/dev/null 2>&1
git -C $G apply --whitespace=nowarn $OUT/$LABEL.diff 2>/dev/null || echo "diff-apply-fail" > $OUT/$LABEL.grade
nice -n 10 $L/env/dj/bin/python $L/grade_dj.py $ID $G >> $OUT/$LABEL.grade
git -C $REPO worktree remove --force $G; git -C $REPO worktree remove --force $W
echo "$LABEL $ARM $ID $(cat $OUT/$LABEL.meta) hits=$(grep -c ',HIT,' $OUT/$LABEL.csv) $(tr '\n' ' ' < $OUT/$LABEL.grade)"
