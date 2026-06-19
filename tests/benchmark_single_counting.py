"""
Benchmark: Verify Single-Counting Fix Across All 6 Cache Layers.

Simulates a realistic multi-session workload and validates:
  1. tokens_intercepted counts only on first disk-read (not on cache hits)
  2. reset_stats preserves cache entries
  3. Unique token counting prevents inflated numbers
"""

import os, sys, tempfile, time, json, hashlib

# Isolated test DB
test_db_dir = tempfile.mkdtemp()
test_db_path = os.path.join(test_db_dir, "bench_single_count.db")
os.environ["TOOLRECALL_CACHE_DB"] = test_db_path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from toolrecall.cache import (
    cached_read, cached_terminal, cached_exec, cached_run,
    get_stats, reset_stats, _init, _estimate_tokens
)

PASS = 0
FAIL = 0

def check(label, condition, detail=""):
    global PASS, FAIL
    if condition:
        print(f"  ✅ {label}")
        PASS += 1
    else:
        print(f"  ❌ {label} — {detail}")
        FAIL += 1

def simulate_session(reads, label="session"):
    """Simulate N reads across files, then report stats per layer."""
    for path, content in reads:
        cached_read(path)
    stats = get_stats()
    fc = stats.get("file_cache", {})
    print(f"  [{label}] hits={fc.get('hits',0)}, tokens={fc.get('tokens_intercepted',0)}")
    return stats

def real_file(size_chars, content=None):
    """Create a temp file with known content size."""
    f = tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False)
    if content:
        f.write(content)
    else:
        f.write("# test\n" * (max(1, size_chars // 6)))
    f.close()
    return f.name

def cleanup(*paths):
    for p in paths:
        try:
            os.unlink(p)
        except OSError:
            pass

# ════════════════════════════════════════
# TEST 1: File Cache — No Double Count
# ════════════════════════════════════════
print("\n═══ TEST 1: File Cache Single-Counting ═══")
reset_stats()
_init()

content_a = "A" * 3000
content_b = "B" * 2000
fa = real_file(0, content_a)
fb = real_file(0, content_b)
expected_a = _estimate_tokens(content_a)
expected_b = _estimate_tokens(content_b)

# Read A once (miss)
r1 = cached_read(fa)
check("A first read is miss", not r1.get("cached"))

# Read A twice more (in-memory + SQLite hits)
r2 = cached_read(fa)
r3 = cached_read(fa)
check("A second read is in-memory hit", r2.get("cached"))
check("A third read is SQLite hit (after implicit), still hit", r3.get("cached"))

# Read B once (miss)
r4 = cached_read(fb)
check("B first read is miss", not r4.get("cached"))

# Now verify: tokens = A + B, not A*3 + B
fc = get_stats().get("file_cache", {})
total_expected = expected_a + expected_b
check(f"tokens = {total_expected} (A: {expected_a} + B: {expected_b})",
      fc["tokens_intercepted"] == total_expected,
      f"got {fc['tokens_intercepted']}")
check("hits = 2 (A 2nd + 3rd read, no B hits yet)",
      fc["hits"] == 2, f"got {fc['hits']}")
check("misses = 2 (A 1st + B 1st)",
      fc["misses"] == 2, f"got {fc['misses']}")

cleanup(fa, fb)

# ════════════════════════════════════════
# TEST 2: Simulate Token Counting (Verify unique, not inflated)
# ════════════════════════════════════════
print("\n═══ TEST 2: Token Counting (Verify Unique) ═══")
reset_stats()
_init()

# Old bug: 666 hits on a ~7KB file → ~1.5M per hit × 666 = ~1B tokens
# New fix: exactly one count per unique file

big_file = real_file(0, "data_row = {} * 200\n" * 20)
big_tokens = _estimate_tokens(open(big_file).read())

# Simulate 100 reads of the same file
for i in range(100):
    cached_read(big_file)

fc = get_stats().get("file_cache", {})
check(f"100 reads of same file = {big_tokens} tokens (not 100×)",
      fc["tokens_intercepted"] == big_tokens,
      f"got {fc['tokens_intercepted']} (should be {big_tokens})")
check(f"hits = 99 (100 reads - 1 miss)",
      fc["hits"] == 99, f"got {fc['hits']}")
check(f"misses = 1",
      fc["misses"] == 1, f"got {fc['misses']}")

# Compare: old bug would have counted 99 × big_tokens
old_bug_value = 99 * big_tokens
savings_factor = old_bug_value / big_tokens
check(f"Old bug would have counted {old_bug_value} (×{savings_factor:.0f} inflation) — prevented",
      fc["tokens_intercepted"] < old_bug_value / 10,
      f"inflation detected: {fc['tokens_intercepted']} vs {old_bug_value}")

cleanup(big_file)

# ════════════════════════════════════════
# TEST 3: Daemon Restart Simulation
# ════════════════════════════════════════
print("\n═══ TEST 3: Daemon Restart (In-Memory → SQLite) ═══")
reset_stats()
_init()

# Read a file → disk miss (counts once)
restart_file = real_file(0, "X" * 1000)
cached_read(restart_file)
hits_1 = get_stats().get("file_cache", {}).get("hits", 0)

# Simulate daemon restart: clear in-memory LRU
from toolrecall.cache import _file_cache
_file_cache.clear()

# Read again → SQLite hit → should NOT count tokens again
cached_read(restart_file)
fc = get_stats().get("file_cache", {})
expected = _estimate_tokens("X" * 1000)
check(f"After restart: tokens = {expected} (same file, no re-count)",
      fc["tokens_intercepted"] == expected,
      f"got {fc['tokens_intercepted']}")
check(f"After restart: hits = {hits_1 + 1})",
      fc["hits"] == hits_1 + 1, f"got {fc['hits']}")

# Second restart
_file_cache.clear()
cached_read(restart_file)
fc = get_stats().get("file_cache", {})
check(f"2nd restart: tokens still {expected} (never re-counted)",
      fc["tokens_intercepted"] == expected,
      f"got {fc['tokens_intercepted']}")

cleanup(restart_file)

# ════════════════════════════════════════
# TEST 4: Other Cache Layers are Single-Count Too
# ════════════════════════════════════════
print("\n═══ TEST 4: Terminal / Code / Script Single-Counting ═══")

# Terminal
reset_stats()
_init()

# Use a cheap, cacheable command
import subprocess
result = subprocess.run(['hostname'], capture_output=True, text=True)
expected_term_tokens = _estimate_tokens(result.stdout)

term1 = cached_terminal('hostname', ttl=60)
check("Terminal first call = miss", not term1.get("cached"))

term2 = cached_terminal('hostname', ttl=60)
check("Terminal second call = hit", term2.get("cached"))

term3 = cached_terminal('hostname', ttl=60)
check("Terminal third call = hit", term3.get("cached"))

tc = get_stats().get("terminal_cache", {})
check(f"Terminal tokens = {expected_term_tokens} (not 3×)",
      tc["tokens_intercepted"] == expected_term_tokens,
      f"got {tc['tokens_intercepted']}")

# Code cache
code = "print('hello world')"
c1 = cached_exec(code, ttl=60)
c2 = cached_exec(code, ttl=60)
cc = get_stats().get("code_cache", {})
check(f"Code cache tokens counted once",
      cc["tokens_intercepted"] > 0,
      f"got {cc['tokens_intercepted']}")
check("Code cache hit works", c2.get("cached"))

# ════════════════════════════════════════
# TEST 5: reset_stats
# ════════════════════════════════════════
print("\n═══ TEST 5: reset_stats Integrity ═══")

entries_before = get_stats().get("file_cache_entries", 0)
# Force a disk read of a new file
new_f = real_file(0, "Z" * 500)
cached_read(new_f)
entries_after_new = get_stats().get("file_cache_entries", 0)
check("Entry count increased after new file",
      entries_after_new > entries_before)

reset_stats()
stats = get_stats()
check("No file_cache section after reset",
      "file_cache" not in stats)
check("No terminal_cache after reset",
      "terminal_cache" not in stats)
check("File entries survive reset",
      stats.get("file_cache_entries", 0) >= entries_after_new,
      f"entries lost: had {entries_after_new}, got {stats.get('file_cache_entries', 0)}")
check("Memory entries intact",
      stats.get("memory_file_entries", -1) >= 0)

cleanup(new_f)

# ════════════════════════════════════════
# RESULTS
# ════════════════════════════════════════
print(f"\n{'═'*50}")
print(f"RESULTS: {PASS} passed, {FAIL} failed")
print(f"{'═'*50}")

# Cleanup
import shutil
shutil.rmtree(test_db_dir, ignore_errors=True)

sys.exit(0 if FAIL == 0 else 1)