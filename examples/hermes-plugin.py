"""ToolRecall — Hermes Plugin for automatic tool caching.

This module can be loaded as an init script to automatically
wrap Hermes tools with cached variants.

Usage in ~/.hermes/config.yaml:
  agent:
    init_scripts:
      - ~/.toolrecall/hermes_plugin.py
"""

try:
    from toolrecall import cached_read, cached_terminal, cached_skill
    from toolrecall.cache import get_stats

    print("ToolRecall plugin loaded. Caching active.")
    print(f"  cached_read    — file reads (mtime-based)")
    print(f"  cached_terminal — commands (TTL-based)")
    print(f"  cached_skill   — skill views (mtime-based)")
    print(f"  cached_run     — script execution (mtime + TTL)")
    print(f"  cached_exec    — Python code (hash + TTL)")
except ImportError:
    print("ToolRecall not installed. Run: pip install toolrecall")