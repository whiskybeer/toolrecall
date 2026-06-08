"""ToolRecall Python Import Example

Level 1 (BEST) Integration:
ToolRecall requires zero network setup and zero subprocess overhead 
if imported directly into an existing Python agent.
"""
from toolrecall.cache import cached_read, cached_terminal, get_stats

# 1. Read a file (Cache Miss -> Hits the disk)
print("Reading file for the first time...")
result1 = cached_read("toolrecall/daemon.py")
print(f"Tokens intercepted: {result1.get('tokens_intercepted')}")
print(f"Was cached: {result1.get('cached')}\n")

# 2. Read it again (Cache Hit -> Served from RAM/SQLite in <0.1ms)
print("Reading file a second time...")
result2 = cached_read("toolrecall/daemon.py")
print(f"Tokens intercepted: {result2.get('tokens_intercepted')}")
print(f"Was cached: {result2.get('cached')}\n")

# 3. View the global statistics
print("Global ToolRecall Stats:")
stats = get_stats()
import json
print(json.dumps(stats, indent=2))
