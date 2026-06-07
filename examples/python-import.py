"""Example: ToolRecall for Hermes — manual import approach.

Usage in any Python script or Hermes session:
    from toolrecall import cached_read, cached_terminal, cached_skill
    from toolrecall import cached_run, cached_exec, docs_search
"""
from toolrecall import cached_read, cached_terminal, cached_skill
from toolrecall import cached_run, cached_exec, docs_search
from toolrecall.cache import get_stats
import json


def demonstrate_file_cache():
    """File reads are cached until the file changes."""
    result = cached_read("README.md")
    print(f"File cache: hit={result['cached']}, {len(result['content'])} chars")


def demonstrate_terminal_cache():
    """Terminal commands with TTL."""
    result = cached_terminal("hostname", ttl=3600)
    print(f"Terminal cache: hit={result['cached']}, output={result['output'].strip()}")


def demonstrate_script_cache():
    """Script execution with mtime + TTL."""
    result = cached_run("scripts/build.sh", "--dry-run", ttl=120)
    print(f"Script cache: hit={result['cached']}")


def demonstrate_code_cache():
    """Python code execution with hash + TTL."""
    code = "import os; print(f'Host: {os.uname().nodename}')"
    result = cached_exec(code, ttl=300)
    print(f"Code cache: hit={result['cached']}, output={result['output'].strip()}")


def show_stats():
    """Print current cache statistics."""
    stats = get_stats()
    total_saved = sum(s["tokens_saved"] for s in stats.values() if isinstance(s, dict))
    print(f"\nTotal tokens saved: {total_saved:,}")
    print(json.dumps(stats, indent=2))


if __name__ == "__main__":
    demonstrate_file_cache()
    demonstrate_terminal_cache()
    demonstrate_script_cache()
    demonstrate_code_cache()
    show_stats()