"""
ToolRecall Framework Adapters — integrate ToolRecall caching into popular
agent frameworks without modifying the framework itself.

Each adapter is a thin wrapper around toolrecall.client (the public API).
The daemon manages the SQLite cache — adapters never open a direct DB
connection, avoiding lock contention.

Available Adapters:
    google_adk    — @cached_tool decorator for ADK @tool functions
    langchain     — ToolRecallCache BaseCache subclass + auto-callback handler
    herdr         — Integration guide for herdr terminal multiplexer

Usage:
    from toolrecall.adapters import google_adk
    from toolrecall.adapters import langchain
    from toolrecall.adapters import herdr
"""

from toolrecall.adapters import google_adk
from toolrecall.adapters import langchain
from toolrecall.adapters import herdr

__all__ = ["google_adk", "langchain", "herdr"]