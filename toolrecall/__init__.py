"""
ToolRecall — Universal Tool-Output Cache for LLM Agents.

Usage:
    from toolrecall import cached_read, cached_skill, cached_terminal, docs_search

    # Instead of read_file():
    result = cached_read('/path/to/file.md')

    # Instead of skill_view():
    skill = cached_skill('skill-name')

    # Instead of terminal('git status'):
    result = cached_terminal('git status')

    # Instead of web search:
    info = docs_search('query')
"""

from toolrecall.cache import cached_read, cached_skill, cached_terminal, cached_run, cached_exec, cached_write, cached_patch, invalidate_all, invalidate_file, refresh_file, cached_mcp_check, cached_mcp_store, cached_mcp, get_stats
from toolrecall.docs import docs_search, docs_get_page
from toolrecall.config import Config
from toolrecall.cli import main as cli_main

__version__ = "0.4.3"
__all__ = [
    "cached_read",
    "cached_skill",
    "cached_terminal",
    "cached_run",
    "cached_exec",
    "cached_write",
    "cached_patch",
    "cached_mcp_check",
    "cached_mcp_store",
    "cached_mcp",
    "docs_search",
    "docs_get_page",
    "invalidate_all",
    "get_stats",
    "Config",
    "cli_main",
]
