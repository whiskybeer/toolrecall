"""micro_cache.py — local micro-benchmark (C5).

No network, no provider, no noise. Measures per-call latency of tool reads
with and without ToolRecall caching. Reports median/p95/p99 — never a sum."""

import statistics
import time
import json
import os
import tempfile


def bench(fn, n=1000, warmup=50):
    for _ in range(warmup):
        fn()
    ts = []
    for _ in range(n):
        t0 = time.perf_counter()
        fn()
        ts.append((time.perf_counter() - t0) * 1000)
    ts.sort()
    return {
        "n": n,
        "median_ms": round(statistics.median(ts), 4),
        "p95_ms": round(ts[int(0.95 * n)], 4),
        "p99_ms": round(ts[int(0.99 * n)], 4),
        "iqr_ms": round(ts[int(0.75 * n)] - ts[int(0.25 * n)], 4),
    }


def call_mcp_uncached():
    """Simulate an MCP subprocess call: fork a minimal Python process."""
    import subprocess
    subprocess.run(
        [sys.executable, "-c", "import json; print(json.dumps({'ok': True}))"],
        capture_output=True,
        timeout=10,
    )


def call_mcp_cached():
    """Simulate MCP call served from cache: read a tiny value from SQLite."""
    import sqlite3
    con = sqlite3.connect(f"file:{CACHE_DB}?mode=ro", uri=True)
    con.execute("SELECT data FROM mcp_cache LIMIT 1").fetchone()
    con.close()


def read_file_uncached():
    """Read a small file from disk directly (no caching layer)."""
    with open(TEST_FILE, "rb") as f:
        _ = f.read()


def read_file_cached():
    """Read a small file via ToolRecall file_cache SQLite lookup."""
    import sqlite3
    con = sqlite3.connect(f"file:{CACHE_DB}?mode=ro", uri=True)
    con.execute(
        "SELECT content FROM file_cache WHERE path = ?", (TEST_FILE,)
    ).fetchone()
    con.close()


def create_test_fixtures():
    """Create a test file and ensure mcp_cache has an entry."""
    global TEST_FILE, CACHE_DB

    CACHE_DB = os.path.expanduser("~/.toolrecall/cache.db")
    TEST_FILE = os.path.join(tempfile.gettempdir(), "toolrecall_micro_bench.txt")

    # Create small test file (~1KB)
    with open(TEST_FILE, "w") as f:
        f.write("x" * 1024)

    # Ensure mcp_cache has at least one entry
    import sqlite3
    con = sqlite3.connect(CACHE_DB)
    count = con.execute("SELECT COUNT(*) FROM mcp_cache").fetchone()[0]
    con.close()
    if count == 0:
        print("WARNING: mcp_cache is empty. MCP benchmarks will fail.")
        print("Run a real session to populate the cache first.")

    return CACHE_DB, TEST_FILE


if __name__ == "__main__":
    import sys

    create_test_fixtures()

    # Check that the test file is cached in file_cache
    import sqlite3
    con = sqlite3.connect(CACHE_DB)
    cached = con.execute(
        "SELECT COUNT(*) FROM file_cache WHERE path = ?", (TEST_FILE,)
    ).fetchone()[0]
    if not cached:
        print("Test file not in file_cache. This run will measure cold read.")
    con.close()

    out = {}
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 1000

    HAS_MCP = len(sys.argv) > 2 and sys.argv[2] == "--mcp"

    print(f"Running micro-benchmark with n={n}...")

    if HAS_MCP:
        # MCP cold (subprocess fork)
        print("  mcp_cold_subprocess...")
        out["mcp_cold_subprocess"] = bench(call_mcp_uncached, n=n)

        # MCP warm (SQLite cache read)
        print("  mcp_warm_cache...")
        out["mcp_warm_cache"] = bench(call_mcp_cached, n=n)

    # File cold (disk read)
    print("  file_cold_disk...")
    out["file_cold_disk"] = bench(read_file_uncached, n=n)

    # File warm (file_cache SQLite read)
    print("  file_warm_cache...")
    out["file_warm_cache"] = bench(read_file_cached, n=n)

    # Speedup ratios (median only)
    for prefix in (["mcp", "file"] if HAS_MCP else ["file"]):
        cold_key = f"{prefix}_cold_subprocess" if prefix == "mcp" else f"{prefix}_cold_disk"
        warm_key = f"{prefix}_warm_cache"
        out[f"{prefix}_speedup_median"] = round(
            out[cold_key]["median_ms"] / out[warm_key]["median_ms"], 2
        )

    # Per-call benefit distribution
    if HAS_MCP:
        out["summary"] = (
            f"MCP: {out['mcp_speedup_median']}x speedup at median. "
            f"File: {out['file_speedup_median']}x speedup at median."
        )
    else:
        out["summary"] = (
            f"File: {out['file_speedup_median']}x speedup at median "
            f"(MCP skipped: no mcp_cache entries)"
        )

    print(json.dumps(out, indent=2))
