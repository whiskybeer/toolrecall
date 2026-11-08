#!/usr/bin/env bash
# run_real.sh ID REP → one Sonnet run of a SWE-bench pytest task through the replay proxy.
# Reuses the earlier cc-ab inst/env/gold/grade infra read-only. FIXED work path per ID
# (cwd is part of the prompt, so it must match across runs).
set -uo pipefail
S="${CC_AB_DIR:?set CC_AB_DIR to the dir holding cc-ab/ (inst, env, gold, grade.sh) and swe-repos/}"
D=$S/cc-ab; REPO=$S/swe-repos/pytest-dev__pytest
ID=$1; REP=$2; I=$D/inst/$ID; OUT=/tmp/trhook/real/results; mkdir -p $OUT
N=${ID}_r$REP; W=/tmp/trhook/real/work/$ID
git -C $REPO worktree remove --force $W 2>/dev/null; rm -rf $W
git -C $REPO worktree add -f --detach $W $(cat $I/base_commit) >/dev/null 2>&1
cp $D/gold/$ID/src/_pytest/_version.py $W/src/_pytest/
PROMPT="You are working in a checkout of the pytest-dev/pytest repository. Fix the GitHub issue below by changing the library source code. Do not modify or add tests under testing/ — hidden tests will grade your fix.

A ready Python environment: run tests with
  PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYTHONPATH=src $D/env/$ID/bin/python -m pytest -p no:cacheprovider <args>

${EXTRA:-}When the fix is complete, stop and give a one-paragraph summary.

<issue>
$(cat $I/problem_statement)
</issue>"
t0=$(date +%s)
(cd $W && env -u CLAUDECODE -u CLAUDE_CODE_SESSION_ID -u CLAUDE_CODE_ENTRYPOINT \
   ANTHROPIC_BASE_URL=http://127.0.0.1:8590 ANTHROPIC_CUSTOM_HEADERS="X-ToolRecall-Canonicalize: claude-code" \
   timeout 2400 claude -p "$PROMPT" --model sonnet --output-format json --dangerously-skip-permissions \
   --strict-mcp-config --mcp-config '{"mcpServers":{}}' < /dev/null > $OUT/$N.json 2> $OUT/$N.err)
echo "rc=$? wall=$(( $(date +%s) - t0 ))" > $OUT/$N.meta
git -C $W add -A -N 2>/dev/null; git -C $W diff -- . ':(exclude)testing' ':(exclude)src/_pytest/_version.py' > $OUT/$N.diff
G=/tmp/trhook/real/grade-$ID; git -C $REPO worktree remove --force $G 2>/dev/null; rm -rf $G
git -C $REPO worktree add -f --detach $G $(cat $I/base_commit) >/dev/null 2>&1
git -C $G apply --whitespace=nowarn $OUT/$N.diff 2>/dev/null || echo "diff-apply-fail" > $OUT/$N.grade
bash $D/grade.sh $ID $G >> $OUT/$N.grade
git -C $REPO worktree remove --force $G; git -C $REPO worktree remove --force $W
echo "$N $(cat $OUT/$N.meta) $(tr '\n' ' ' < $OUT/$N.grade)"
