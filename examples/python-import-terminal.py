"""ToolRecall Python Import Example (Terminal Caching)

Level 1 (BEST) Integration:
This script demonstrates how to execute standard terminal commands
via ToolRecall, utilizing the cache to save execution time.

WARNING: To run this example, you must enable `allow_terminal = true`
in your ~/.toolrecall/config.toml, as terminal caching is a disabled
security risk by default.
"""
from toolrecall.cache import cached_terminal, get_stats
import time

# 1. Run a command (Cache Miss -> Subprocess Execution)
print("Executing 'ls -la' for the first time...")
t0 = time.time()
result1 = cached_terminal("ls -la", ttl=30)
t1 = time.time()
print(f"Was cached: {result1.get('cached')}")
print(f"Time taken: {t1-t0:.4f} seconds\n")

# 2. Run it again (Cache Hit -> Served from SQLite)
print("Executing 'ls -la' a second time (within TTL)...")
t0 = time.time()
result2 = cached_terminal("ls -la", ttl=30)
t1 = time.time()
print(f"Was cached: {result2.get('cached')}")
print(f"Time taken: {t1-t0:.4f} seconds\n")
print("Notice how the execution time drops to <0.002s by bypassing os.fork()")
