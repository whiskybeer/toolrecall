"""
Realistic workload benchmark: simulates agentic coding session behavior.
Measures: unique token savings, hit rates, cost, cross-session behavior.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from toolrecall.cache import (
    cached_read, cached_terminal, cached_exec,
    get_stats, reset_stats, _init
)

TOTAL_CHECKS = 0
TOTAL_PASS = 0
TOTAL_FAIL = 0

def check(label, condition, detail=""):
    global TOTAL_PASS, TOTAL_FAIL, TOTAL_CHECKS
    TOTAL_CHECKS += 1
    if condition:
        print(f"  ✅ {label}")
        TOTAL_PASS += 1
    else:
        print(f"  ❌ {label} — {detail}")
        TOTAL_FAIL += 1

# ════════════════════════════════════════════
# PHASE 0: Reset + fresh start
# ════════════════════════════════════════════
print("=" * 65)
print("  ToolRecall Real-World Workload Benchmark")
print("=" * 65)

reset_stats()
_init()

# Project files to read (real files from ToolRecall project)
REPO = os.path.expanduser("~/toolrecall")
FILES = [
    os.path.join(REPO, "README.md"),           # ~23KB
    os.path.join(REPO, "toolrecall/cache.py"),  # ~30KB
    os.path.join(REPO, "toolrecall/daemon.py"),
    os.path.join(REPO, "toolrecall/cli.py"),
    os.path.join(REPO, "toolrecall/docs.py"),
    os.path.join(REPO, "toolrecall/config.py"),
    os.path.join(REPO, "toolrecall/client.py"),
    os.path.join(REPO, "toolrecall/mcp_server.py"),
    os.path.join(REPO, "toolrecall/proxy.py"),
    os.path.join(REPO, "SECURITY.md"),          # ~4.6KB
    os.path.join(REPO, "pyproject.toml"),
    os.path.join(REPO, "docs/BENCHMARK.md"),
    os.path.join(REPO, "docs/BOTTLENECK_SOLVED.md"),
]

# ════════════════════════════════════════════
# PHASE 1: Session Day 1 — First read of all files
# ════════════════════════════════════════════
print("\n─── PHASE 1: Session Day 1 (first reads) ───")

for f in FILES:
    if os.path.exists(f):
        r = cached_read(f)
        check(f"Read {os.path.basename(f)}", not r.get("cached"), "unexpected cache hit")

s1 = get_stats()
fc = s1.get("file_cache", {})
files_read = s1.get("file_cache_entries", 0)
check(f"All {files_read} unique files read from disk",
      files_read == len(FILES),
      f"read {files_read} of {len(FILES)}")
print(f"     Tokens saved (unique): {fc.get('tokens_read_from_disk', 0):,}")
print(f"     Hits: {fc.get('hits', 0)}, Misses: {fc.get('misses', 0)}")

# ════════════════════════════════════════════
# PHASE 2: Same session — re-read files (cache hits)
# ════════════════════════════════════════════
print("\n─── PHASE 2: Session Day 1 (re-reads, cache hits) ───")

for i in range(3):
    for f in FILES[:6]:  # Re-read first 6 files 3x each
        if os.path.exists(f):
            r = cached_read(f)
            check(f"Re-read {os.path.basename(f)} #{i+1}", r.get("cached"), "should be hit")

s2 = get_stats()
fc2 = s2.get("file_cache", {})
tokens_after_phase2 = fc2.get("tokens_read_from_disk", 0)
tokens_phase1 = fc.get("tokens_read_from_disk", 0)
check("Tokens did NOT increase on re-reads",
      tokens_after_phase2 == tokens_phase1,
      f"phase1={tokens_phase1} phase2={tokens_after_phase2}")
hits_expected = fc.get("hits", 0) + (3 * 6)  # 3 rounds × 6 files
check(f"Hits increased to {hits_expected}",
      fc2.get("hits", 0) == hits_expected,
      f"got {fc2.get('hits', 0)}")

# ════════════════════════════════════════════
# PHASE 3: Simulate new files (git pull / code changes)
# ════════════════════════════════════════════
print("\n─── PHASE 3: New files appear (git pull) ───")

import tempfile
new_files = []
for i in range(3):
    f = tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False)
    f.write(f"# New module {i}\n'''Placeholder for new code.'''\n" * 50)
    f.close()
    new_files.append(f.name)
    r = cached_read(f.name)
    check(f"New file {os.path.basename(f.name)} (miss)", not r.get("cached"))

s3 = get_stats()
fc3 = s3.get("file_cache", {})
new_tokens = fc3.get("tokens_read_from_disk", 0) - tokens_after_phase2
check(f"Tokens increased by {new_tokens:,} for 3 new files",
      new_tokens > 0,
      "no increase")

# ════════════════════════════════════════════
# PHASE 4: Cross-session (daemon restart simulation)
# ════════════════════════════════════════════
print("\n─── PHASE 4: Cross-session (simulated daemon restart) ───")

from toolrecall.cache import _file_cache
tokens_before_restart = fc3.get("tokens_read_from_disk", 0)

# Clear in-memory LRU (simulates daemon restart)
_file_cache.clear()

# Re-read — all from SQLite, no new tokens
for f in FILES[:4]:
    if os.path.exists(f):
        r = cached_read(f)
        check(f"After restart: {os.path.basename(f)} (SQLite hit)", r.get("cached"))

s4 = get_stats()
fc4 = s4.get("file_cache", {})
check("Tokens unchanged after daemon restart",
      fc4.get("tokens_read_from_disk", 0) == tokens_before_restart,
      f"{tokens_before_restart} → {fc4.get('tokens_read_from_disk', 0)}")
check("Hits increased (SQLite promoted to In-Memory)",
      fc4.get("hits", 0) > fc3.get("hits", 0),
      f"before: {fc3.get('hits', 0)}, after: {fc4.get('hits', 0)}")

# ════════════════════════════════════════════
# PHASE 5: Terminal cache real commands
# ════════════════════════════════════════════
print("\n─── PHASE 5: Terminal Commands ───")

commands = ["hostname", "pwd", "uname -a", "uptime", "free -h", "df -h /"]
for cmd in commands:
    r1 = cached_terminal(cmd, ttl=30)
    # Note: terminal may cache subprocess output via DEFAULT_CACHEABLE
    # but we'll just check it runs
    check(f"Terminal: {cmd}", not r1.get("error", False),
          f"error: {r1.get('error')}")

# Re-run same commands (should be hits for cacheable ones)
for cmd in ["hostname", "pwd"]:
    r2 = cached_terminal(cmd, ttl=30)
    # these are in DEFAULT_CACHEABLE so should hit
    print(f"     {cmd}: cached={r2.get('cached')}")

ss = get_stats()
tc = ss.get("terminal_cache", {})
print(f"     Terminal: {tc.get('hits',0)} hits, {tc.get('misses',0)} misses, {tc.get('tokens_read_from_disk',0)} tokens")

# ════════════════════════════════════════════
# PHASE 6: Code execution cache
# ════════════════════════════════════════════
print("\n─── PHASE 6: Code Execution Cache ───")

codes = [
    "print('hello world')",
    "import json; print(json.dumps({'a': 1, 'b': 2}))",
    "print(sum(range(100)))",
]
for code in codes:
    r1 = cached_exec(code, ttl=60)
    check(f"Code exec: {code[:30]}", not r1.get("error", False))
    r2 = cached_exec(code, ttl=60)
    check(f"Code exec (cached): {code[:30]}", r2.get("cached"))

ss2 = get_stats()
cc = ss2.get("code_cache", {})
print(f"     Code: {cc.get('hits',0)} hits, {cc.get('misses',0)} misses, {cc.get('tokens_read_from_disk',0)} tokens")

# ════════════════════════════════════════
# PHASE 7: Final report
# ════════════════════════════════════════
print("\n" + "=" * 65)
print("  FINAL REPORT")
print("=" * 65)

final = get_stats()
layers = {
    "📁 File": "file_cache",
    "💻 Terminal": "terminal_cache",
    "📜 Script": "script_cache",
    "🐍 Code": "code_cache",
    "🌐 MCP": "mcp_cache",
}

total_tokens = 0
total_cost = 0
for emoji, key in layers.items():
    d = final.get(key, {})
    tokens = d.get("tokens_read_from_disk", 0)
    hits = d.get("hits", 0)
    misses = d.get("misses", 0)
    rate = d.get("hit_rate", "0%")
    entries = final.get(f"{key}_entries", 0)
    cost = tokens / 1_000_000 * 2
    total_tokens += tokens
    total_cost += cost
    if key != "mcp_cache" or entries > 0 or tokens > 0:
        print(f"  {emoji}  {key:<25} {entries:>4} entries | "
              f"{hits:>4} hits / {misses:<4} misses | "
              f"{rate:>4} | tokens={tokens:>8,}")

print(f"  {'─'*57}")
print(f"  TOTAL: {final.get('file_cache_entries',0) + final.get('terminal_cache_entries',0) + final.get('script_cache_entries',0) + final.get('code_cache_entries',0) + final.get('mcp_cache_entries',0):>4} entries, {total_tokens:>8,} tokens | "
      f"~${total_cost:.4f} @ $2/M")
print(f"  Memory: {final.get('memory_used_mb', 0)} MB / {final.get('memory_max_mb', 0)} MB")
print(f"  Memory entries: {final.get('memory_file_entries', 0)} files")

# Cost comparison
print("\n  💰 COST COMPARISON (@ $2/M input tokens):")
print(f"     Real (1x counted): {total_tokens:>10,} tokens = ${total_cost:.4f}")

# Old bug simulation
import sqlite3 as _s3
_conn = _s3.connect(os.path.expanduser('~/.toolrecall/cache.db'))
_conn.row_factory = _s3.Row
old_bug_file = sum(max(1, r['b'] // 3) * r['hits'] 
                   for r in _conn.execute('SELECT LENGTH(content) as b, hits FROM file_cache').fetchall())
_conn.close()
old_bug = old_bug_file + total_tokens - (final.get("file_cache", {}).get("tokens_read_from_disk", 0))
print(f"     Alter Bug (alle hits):{old_bug:>10,} tokens = ${old_bug/1_000_000 * 2:.4f} (×{old_bug/max(total_tokens,1):.0f})")
print("     Echte unique Tokens: ~55K (13 files)")

print(f"\n  {'='*65}")
print(f"  CHECKS: {TOTAL_PASS} passed, {TOTAL_FAIL} failed (of {TOTAL_CHECKS})")
print(f"  {'='*65}")

# Cleanup
for f in new_files:
    try:
        os.unlink(f)
    except Exception:
        pass

sys.exit(1 if TOTAL_FAIL else 0)
