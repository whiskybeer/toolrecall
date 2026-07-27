#!/usr/bin/env bash
# ToolRecall Claude Code Integration Test Runner
# Usage: cd tests/claude-integration && bash run-test.sh
set -euo pipefail

TEST_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "$TEST_DIR/../.." && pwd)"
CACHE_DB="${TOOLRECALL_CACHE_DB:-$HOME/.toolrecall/cache.db}"
REPORT_FILE="$TEST_DIR/report.txt"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
PASS=0
FAIL=0

echo "================================================"
echo "  ToolRecall × Claude Code Integration Test"
echo "  $TIMESTAMP"
echo "================================================"
echo ""

# ── Step 1: Check prerequisites ──
echo "[1/6] Checking prerequisites..."
PREREQS_OK=true

if command -v toolrecall &>/dev/null; then
  echo "  ✅ toolrecall CLI found: v$(toolrecall --version 2>/dev/null || echo '?')"
else
  echo "  ❌ toolrecall not in PATH"
  echo "     Install: pip install toolrecall"
  PREREQS_OK=false
fi

if command -v claude &>/dev/null; then
  VER=$(claude --version 2>/dev/null || echo '?')
  echo "  ✅ Claude Code CLI found: $VER"
else
  echo "  ❌ claude command not found"
  echo "     Install: https://docs.anthropic.com/en/docs/claude-code/overview"
  PREREQS_OK=false
fi

# Check daemon
DAEMON_OK=false
if toolrecall daemon --status 2>/dev/null | grep -q "pid"; then
  DAEMON_OK=true
  echo "  ✅ ToolRecall daemon is running"
else
  echo "  ⚠️  ToolRecall daemon not running"
  echo "     Start: toolrecall daemon start"
  # Not fatal — we can still check the DB
fi

if ! $PREREQS_OK; then
  echo ""
  echo "❌ Prerequisites not met. Fix above and re-run."
  exit 1
fi

# ── Step 2: Record pre-test cache state ──
echo ""
echo "[2/6] Recording pre-test cache state..."

PRE_HITS=0
PRE_MISSES=0
PRE_OUR=0

if [ -f "$CACHE_DB" ]; then
  PRE_HITS=$(sqlite3 "$CACHE_DB" "SELECT COALESCE(SUM(hit), 0) FROM access_log WHERE category='file_cache';" 2>/dev/null || echo "0")
  PRE_MISSES=$(sqlite3 "$CACHE_DB" "SELECT COALESCE(SUM(miss), 0) FROM access_log WHERE category='file_cache';" 2>/dev/null || echo "0")
  PRE_OUR=$(sqlite3 "$CACHE_DB" "SELECT COUNT(*) FROM access_log WHERE category='file_cache' AND path LIKE '%claude-integration%';" 2>/dev/null || echo "0")
  echo "  Cache hits before:  $PRE_HITS"
  echo "  Cache misses before: $PRE_MISSES"
  echo "  Existing test entries: $PRE_OUR"
else
  echo "  No cache DB yet (fresh start)"
fi

# ── Step 3: Write a minimal CLAUDE.md Claude will see ──
echo ""
echo "[3/6] Writing CLAUDE.md for test context..."

cat > "$TEST_DIR/CLAUDE.md" << 'CLAUDEMD'
# ToolRecall Integration Test

You are in a test project. You have MCP tools available including 
`read_file` (cached_read), `write_file`, and `terminal` from ToolRecall.

## Task (multi-turn)

Read the following files and tell me what each contains:

1. Read src/config.js — report the default port
2. Read src/utils.js — report how many functions are exported
3. Read src/main.js — report what routes are registered
4. Now read src/config.js AGAIN — report the port (should be same)
5. Now read test-file.txt — report what the TR marker value is
6. Read src/config.js a THIRD time — report the port again

Read each file fresh each time — do not rely on memory.
Report your findings after each read.
CLAUDEMD
echo "  ✅ CLAUDE.md written"

# ── Step 4: Configure MCP for this test ──
echo ""
echo "[4/6] Setting up MCP config..."

# Back up existing Claude settings if any
CC_SETTINGS="$HOME/.claude/settings.json"
CC_SETTINGS_BAK="$HOME/.claude/settings.json.trtest.bak"
if [ -f "$CC_SETTINGS" ]; then
  cp "$CC_SETTINGS" "$CC_SETTINGS_BAK"
  echo "  ✅ Backed up existing Claude settings"
fi

# Write test config that ONLY has ToolRecall MCP
mkdir -p "$HOME/.claude"
cat > "$CC_SETTINGS" << 'SETTINGS'
{
  "mcpServers": {
    "toolrecall": {
      "command": "toolrecall",
      "args": ["mcp"]
    }
  }
}
SETTINGS
echo "  ✅ Written Claude MCP config (ToolRecall only)"

# ── Step 5: Run Claude Code ──
echo ""
echo "[5/6] Running Claude Code..."
echo "  Directory: $TEST_DIR"
echo "  This will take ~1-3 minutes..."
echo ""

START_TIME=$(date +%s)
set +e
CLAUDE_OUTPUT=$(cd "$TEST_DIR" && claude -p "$(cat "$TEST_DIR/CLAUDE.md")" 2>&1)
CLAUDE_EXIT=$?
set -e
END_TIME=$(date +%s)
ELAPSED=$((END_TIME - START_TIME))

echo "  Claude exit code: $CLAUDE_EXIT"
echo "  Duration: ${ELAPSED}s"

# Save output
echo "$CLAUDE_OUTPUT" > "$TEST_DIR/claude-output-${TIMESTAMP}.txt"
echo "  Output saved to: claude-output-${TIMESTAMP}.txt"

# ── Step 6: Check cache DB for results ──
echo ""
echo "[6/6] Analyzing results..."
echo ""

if [ ! -f "$CACHE_DB" ]; then
  echo "  ❌ No cache DB at $CACHE_DB"
  echo "  RESULT: FAIL — daemon may not be caching to SQLite"
  RESULTS="FAIL"
else
  POST_HITS=$(sqlite3 "$CACHE_DB" "SELECT COALESCE(SUM(hit), 0) FROM access_log WHERE category='file_cache';" 2>/dev/null || echo "0")
  POST_MISSES=$(sqlite3 "$CACHE_DB" "SELECT COALESCE(SUM(miss), 0) FROM access_log WHERE category='file_cache';" 2>/dev/null || echo "0")
  
  # Read actual access log entries for our test files
  ACCESS_LOG=$(sqlite3 -header -separator ' | ' "$CACHE_DB" "SELECT path, hit, cached_at FROM access_log WHERE category='file_cache' AND path LIKE '%claude-integration%' ORDER BY cached_at;" 2>/dev/null || echo "No entries found")
  OUR_TOTAL=$(sqlite3 "$CACHE_DB" "SELECT COUNT(*) FROM access_log WHERE category='file_cache' AND path LIKE '%claude-integration%';" 2>/dev/null || echo "0")
  OUR_HITS=$(sqlite3 "$CACHE_DB" "SELECT COUNT(*) FROM access_log WHERE category='file_cache' AND path LIKE '%claude-integration%' AND hit=1;" 2>/dev/null || echo "0")
  OUR_MISSES=$(sqlite3 "$CACHE_DB" "SELECT COUNT(*) FROM access_log WHERE category='file_cache' AND path LIKE '%claude-integration%' AND hit=0;" 2>/dev/null || echo "0")
  
  DELTA_HITS=$((POST_HITS - PRE_HITS))
  DELTA_MISSES=$((POST_MISSES - PRE_MISSES))
  
  echo "  ┌─────────────────────────────────────────────────┐"
  echo "  │                 CACHE STATISTICS                │"
  echo "  ├──────────────────────────┬──────────────────────┤"
  printf "  │ %-24s │ %20s │\n" "Pre-test hits" "$PRE_HITS"
  printf "  │ %-24s │ %20s │\n" "Post-test hits" "$POST_HITS"
  printf "  │ %-24s │ %20s │\n" "Delta" "$DELTA_HITS"
  echo "  ├──────────────────────────┼──────────────────────┤"
  printf "  │ %-24s │ %20s │\n" "Pre-test misses" "$PRE_MISSES"
  printf "  │ %-24s │ %20s │\n" "Post-test misses" "$POST_MISSES"
  printf "  │ %-24s │ %20s │\n" "Delta" "$DELTA_MISSES"
  echo "  ├──────────────────────────┼──────────────────────┤"
  printf "  │ %-24s │ %20s │\n" "Test file entries" "$OUR_TOTAL"
  printf "  │ %-24s │ %20s │\n" "  of which hits" "$OUR_HITS"
  printf "  │ %-24s │ %20s │\n" "  of which misses" "$OUR_MISSES"
  echo "  └──────────────────────────┴──────────────────────┘"
  echo ""
  
  # Access log
  if [ "$OUR_TOTAL" -gt 0 ]; then
    echo "  Test file access log:"
    echo "$ACCESS_LOG" | while IFS= read -r line; do
      echo "    $line"
    done
  else
    echo "  No ToolRecall access entries for these test files."
    echo "  Claude Code did NOT call MCP cached_read for any test file."
  fi
  echo ""
  
  # Determine result
  if [ "$OUR_HITS" -gt 0 ]; then
    RESULTS="PASS"
    echo "  ✅ PASS — Claude Code used MCP cached_read"
    echo "     $OUR_HITS cache hits on test files. File caching is viable."
  elif [ "$OUR_TOTAL" -gt 0 ] && [ "$OUR_HITS" -eq 0 ]; then
    RESULTS="PARTIAL"
    echo "  ⚠️  PARTIAL — MCP was called but all misses (first reads)"
    echo "     Run the test again to see if repeat reads hit cache."
  elif [ "$OUR_TOTAL" -eq 0 ] && [ "$DELTA_HITS" -gt 0 ]; then
    RESULTS="INCONCLUSIVE"
    echo "  ⚠️  INCONCLUSIVE — Other files cached but not test files"
    echo "     Claude may have used MCP tools for different files."
  elif [ "$DELTA_HITS" -eq 0 ] && [ "$DELTA_MISSES" -eq 0 ]; then
    RESULTS="FAIL"
    echo "  ❌ FAIL — No cache activity at all"
    echo "     Claude Code used native Read exclusively."
  else
    RESULTS="UNCLEAR"
    echo "  ?  UNCLEAR — See raw numbers above"
  fi
fi

# ── Restore original Claude settings ──
echo ""
echo "Cleaning up..."
if [ -f "$CC_SETTINGS_BAK" ]; then
  mv "$CC_SETTINGS_BAK" "$CC_SETTINGS"
  echo "  ✅ Restored original Claude settings"
else
  rm -f "$CC_SETTINGS"
  echo "  ✅ Removed test Claude settings (no original existed)"
fi

# ── Write report ──
cat > "$REPORT_FILE" << REPORT
ToolRecall × Claude Code Integration Test Report
=================================================
Date:        $TIMESTAMP
Duration:    ${ELAPSED}s
Claude:      $(claude --version 2>/dev/null || echo 'unknown')
ToolRecall:  v$(toolrecall --version 2>/dev/null || echo 'unknown')

Result: $RESULTS

Cache Statistics:
  Pre-test hits:       $PRE_HITS
  Post-test hits:      $POST_HITS  (+$DELTA_HITS)
  Pre-test misses:     $PRE_MISSES
  Post-test misses:    $POST_MISSES  (+$DELTA_MISSES)

Test File Access (path | hit | time):
$ACCESS_LOG

Interpretation:
  PASS        → Claude Code used MCP cached_read. File caching is viable.
  PARTIAL     → MCP called but all first-time reads. Re-run for repeat data.
  FAIL        → No MCP calls. Native Read used exclusively.
  INCONCLUSIVE → See access log manually.

Claude output saved to: claude-output-${TIMESTAMP}.txt
REPORT

echo ""
echo "================================================"
echo "  RESULT: $RESULTS"
echo "  Report: $REPORT_FILE"
echo "================================================"