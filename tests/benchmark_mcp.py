"""MCP Cache Benchmark: cached_mcp() vs Direct MCP Calls.

Measures latency, tokens, and hit/miss behavior for MCP tool calls
using simulated MCP calls (no live server needed).

Usage:
  python3 -m pytest tests/benchmark_mcp.py -v -s   # Run via pytest
  python3 tests/benchmark_mcp.py                     # Run standalone
"""

import time
import sys
import os
import json

# Force isolated test DB
import tempfile

test_dir = tempfile.mkdtemp()
os.environ["TOOLRECALL_CACHE_DB"] = os.path.join(test_dir, "bench_cache.db")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from toolrecall.cache import cached_mcp_check, cached_mcp_store, cached_mcp
from toolrecall.cache import _init, get_stats


def _estimate_tokens(text: str) -> int:
    return max(1, len(text) // 3) if text else 0


def simulate_mcp_fetch(server, tool, args):
    """Simulate an MCP server call with realistic latency (150ms)."""
    time.sleep(0.15)
    data = {"result": f"{server}/{tool} response", "args": args}
    return json.dumps(data, indent=2)


def run_benchmark(count=5):
    """Run benchmark: N rounds, measure miss vs hit."""
    _init()

    server = "bench"
    tool = "echo"
    args = {"msg": "hello"}
    ttl = 60

    results = {
        "server": server,
        "tool": tool,
        "arguments": args,
        "ttl": ttl,
        "response_size_chars": 0,
        "response_size_tokens": 0,
        "test_rounds": count,
        "miss_times_ms": [],
        "hit_times_ms": [],
        "miss_avg_ms": 0.0,
        "hit_avg_ms": 0.0,
        "speedup": 0.0,
    }

    # Pre-fill cache
    check = cached_mcp_check(server, tool, args, ttl=ttl)
    data = simulate_mcp_fetch(server, tool, args)
    cached_mcp_store(check["key"], server, tool, args, data, ttl=ttl)
    results["response_size_chars"] = len(data)
    results["response_size_tokens"] = _estimate_tokens(data)

    for i in range(count):
        start = time.perf_counter()
        hit = cached_mcp_check(server, tool, args, ttl=ttl)
        elapsed = (time.perf_counter() - start) * 1000
        assert hit["cached"], f"Round {i}: expected cache hit"
        results["hit_times_ms"].append(round(elapsed, 4))

    for i in range(count):
        unique_args = {"msg": f"hello_{i}"}
        start = time.perf_counter()
        check = cached_mcp_check(server, tool, unique_args, ttl=0)
        data = simulate_mcp_fetch(server, tool, unique_args)
        cached_mcp_store(check["key"], server, tool, unique_args, data, ttl=60)
        elapsed = (time.perf_counter() - start) * 1000
        results["miss_times_ms"].append(round(elapsed, 4))

    results["hit_avg_ms"] = (
        round(sum(results["hit_times_ms"]) / len(results["hit_times_ms"]), 4)
        if results["hit_times_ms"]
        else 0.0
    )
    results["miss_avg_ms"] = (
        round(sum(results["miss_times_ms"]) / len(results["miss_times_ms"]), 4)
        if results["miss_times_ms"]
        else 0.0
    )
    results["speedup"] = (
        round(results["miss_avg_ms"] / results["hit_avg_ms"], 1)
        if results["hit_avg_ms"]
        else 0.0
    )

    stats = get_stats()
    mcp_stats = stats.get("mcp_cache", {})
    results["mcp_cache_hits"] = mcp_stats.get("hits", 0)
    results["mcp_cache_misses"] = mcp_stats.get("misses", 0)
    results["tokens_saved"] = mcp_stats.get("tokens_saved", 0)

    # One-shot cached_mcp() benchmark
    start = time.perf_counter()
    cached_mcp(
        "test",
        "echo",
        {"msg": "hello"},
        fetch_fn=lambda: simulate_mcp_fetch("test", "echo", {"msg": "hello"}),
        ttl=1,
    )
    oneshot_miss = (time.perf_counter() - start) * 1000

    start = time.perf_counter()
    cached_mcp(
        "test",
        "echo",
        {"msg": "hello"},
        fetch_fn=lambda: simulate_mcp_fetch("test", "echo", {"msg": "hello"}),
        ttl=1,
    )
    oneshot_hit = (time.perf_counter() - start) * 1000

    results["cached_mcp_miss_ms"] = round(oneshot_miss, 4)
    results["cached_mcp_hit_ms"] = round(oneshot_hit, 4)

    return results


def test_benchmark_basic():
    """Run benchmark — prove hits are faster than misses."""
    results = run_benchmark(count=5)
    assert results["speedup"] > 1, f"Expected speedup >1, got {results['speedup']}"
    assert results["hit_avg_ms"] < results["miss_avg_ms"], (
        f"Hits ({results['hit_avg_ms']}ms) must be faster than "
        f"misses ({results['miss_avg_ms']}ms)"
    )
    assert results["hit_avg_ms"] < 5, (
        f"Cache hits should be <5ms, got {results['hit_avg_ms']}ms"
    )
    print(
        f"  PASS: {results['speedup']}x speedup "
        f"(hit: {results['hit_avg_ms']:.2f}ms vs "
        f"miss: {results['miss_avg_ms']:.2f}ms)"
    )


if __name__ == "__main__":
    print("=" * 60)
    print("ToolRecall MCP Cache Benchmark (simulated)")
    print("=" * 60)
    print("  Using simulated MCP call (150ms latency)")
    print()

    results = run_benchmark(count=10)

    print(f"  Server:         {results['server']}")
    print(f"  Tool:           {results['tool']}")
    print(f"  Response size:  {results['response_size_chars']} chars "
          f"({results['response_size_tokens']} tokens)")
    print(f"  Rounds:         {results['test_rounds']}")
    print()
    print("  +----------------------+--------------+--------------+")
    print("  | Metric               | Cache MISS    | Cache HIT     |")
    print("  +----------------------+--------------+--------------+")
    print(f"  | Avg time (check+     | {results['miss_avg_ms']:>10.4f} ms   "
          f"| {results['hit_avg_ms']:>10.4f} ms   |")
    print("  | fetch/store)         |              |              |")
    print("  +----------------------+--------------+--------------+")
    print(f"  | Fastest              | {min(results['miss_times_ms']):>10.4f} ms   "
          f"| {min(results['hit_times_ms']):>10.4f} ms   |")
    print(f"  | Slowest              | {max(results['miss_times_ms']):>10.4f} ms   "
          f"| {max(results['hit_times_ms']):>10.4f} ms   |")
    print("  +----------------------+--------------+--------------+")
    print(f"  | Speedup              | --           | {results['speedup']}x           |")
    print(f"  | Tokens saved         | --           | {results['tokens_saved']}               |")
    print("  +----------------------+--------------+--------------+")
    print()
    print("  cached_mcp() one-shot:")
    print(f"    Miss (check+fetch+store): {results['cached_mcp_miss_ms']:.4f} ms")
    print(f"    Hit  (cache only):        {results['cached_mcp_hit_ms']:.4f} ms")
    print()
    summary = (
        f"SUMMARY: MCP Cache | "
        f"Miss avg: {results['miss_avg_ms']:.4f}ms | "
        f"Hit avg: {results['hit_avg_ms']:.4f}ms | "
        f"Speedup: {results['speedup']}x | "
        f"Tokens saved: {results['tokens_saved']}"
    )
    print(f"  {summary}")
    print("=" * 60)

    # Save results for CI parsing
    results_file = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), ".mcp_benchmark_results.json"
    )
    with open(results_file, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\n  Results saved to: {results_file}")
