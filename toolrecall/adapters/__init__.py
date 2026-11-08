# Copyright (c) 2026 Robin Schultka
# SPDX-License-Identifier: MIT
# Source: https://github.com/whiskybeer/toolrecall
"""ToolRecall Framework Adapters — integrate ToolRecall caching into popular
agent frameworks without modifying the framework itself.

Each adapter is a thin wrapper around toolrecall.client (the public API).
The daemon manages the SQLite cache — adapters never open a direct DB
connection, avoiding lock contention.

|Available Adapters:
    google_adk    — @cached_tool decorator for ADK @tool functions
    antigravity   — @cached_tool decorator for Antigravity SDK tools
    langchain     — ToolRecallCache BaseCache subclass + auto-callback handler
    langgraph     — ToolRecallCheckpointSaver (BaseCheckpointSaver) + @cached_tool
    langfuse      — @cached_tool decorator reporting cache verdicts into Langfuse traces
    herdr         — Integration guide for herdr terminal multiplexer
    odysseus      — Agent tool + MCP server cache for Odysseus AI workspace
    litellm       — Pre-call content dedup hook for LiteLLM proxy

See README.md in this directory for detailed docs, examples, and architecture.

Usage:
    from toolrecall.adapters import google_adk
    from toolrecall.adapters import antigravity
    from toolrecall.adapters import langchain
    from toolrecall.adapters import herdr
    from toolrecall.adapters import odysseus
    from toolrecall.adapters import litellm
"""

import importlib
from typing import Any

# Adapter module registry: lazy-loaded via PEP 562 __getattr__ so importing
# a single adapter (e.g. claude_code or litellm) has near-zero cold-start overhead.
_MODULES = {
    "antigravity",
    "claude_code",
    "google_adk",
    "langchain",
    "langgraph",
    "langfuse",
    "herdr",
    "odysseus",
    "openevolve",
    "litellm",
    "warp",
    "gepa",
}

__all__ = sorted(_MODULES)


def __getattr__(name: str) -> Any:
    if name in _MODULES:
        return importlib.import_module(f"toolrecall.adapters.{name}")
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__():
    return sorted(list(globals().keys()) + list(_MODULES))
