"""
ToolRecall — Universal Tool-Output Cache for LLM Agents.
"""

# lazy imports — avoid loading cache (and its DB connection) at import time.
# This prevents SQLite lock conflicts when both daemon and client open the DB.
# Each submodule is imported on first access via __getattr__.

__version__ = "0.8.0"


def __getattr__(name):
    """Lazy import on attribute access."""
    if name in (
        "cached_read", "cached_skill", "cached_terminal",
        "cached_run", "cached_exec", "cached_write", "cached_patch",
        "invalidate_all", "invalidate_file", "refresh_file",
        "cached_mcp_check", "cached_mcp_store", "cached_mcp",
        "get_stats",
    ):
        from toolrecall import cache as _mod
        return getattr(_mod, name)
    if name in ("docs_search", "docs_get_page"):
        from toolrecall import docs as _mod
        return getattr(_mod, name)
    if name == "Config":
        from toolrecall.config import Config
        return Config
    if name == "cli_main":
        from toolrecall.cli import main
        return main
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "cached_read", "cached_skill", "cached_terminal",
    "cached_run", "cached_exec", "cached_write", "cached_patch",
    "cached_mcp_check", "cached_mcp_store", "cached_mcp",
    "docs_search", "docs_get_page",
    "invalidate_all", "get_stats",
    "Config", "cli_main",
]
