"""
Google ADK Adapter — @cached_tool decorator for ADK @tool functions.

The Python shim already caches file reads (open()) and terminal commands
(subprocess.run()) at the interpreter level. This adapter fills the gap:
ADK @tool functions that make API calls, search queries, or database
lookups — code the shim can't see.

Usage:
    from toolrecall.adapters import google_adk
    from google.adk import tool

    @tool
    @google_adk.cached_tool(ttl=300)
    def search_web(query: str) -> str:
        # This runs only on cache miss
        return external_api_call(query)

    # On repeat calls with the same query → cached result returned
    # in ~0.6ms, no API call made.

Architecture:
    cached_tool wraps toolrecall.cache.cached_mcp_check / cached_mcp_store.
    The adapter "server" name is "adk" — cache keys are namespaced so
    they don't collide with other frameworks.

    The decorator is a thin (~30 line) wrapper. No framework
    monkey-patching, no middleware pipeline, no dependency on ADK
    internals. Works with any ADK version.
"""

import asyncio
import functools
import logging

from toolrecall.cache import cached_mcp_check, cached_mcp_store
from toolrecall.client import daemon_running

logger = logging.getLogger("toolrecall.adapters.google_adk")

ADAPTER_SERVER = "adk"  # Cache namespace for ADK tool calls


def cached_tool(func=None, *, ttl: int = None):
    """Decorator: cache ADK @tool function results via ToolRecall.

    Can be used with or without arguments:
        @cached_tool          # no args, default TTL
        @cached_tool(ttl=300) # custom TTL in seconds

    The decorator wraps the function to check the ToolRecall cache
    before executing the real function. On cache hit, the cached
    result is returned immediately. On miss, the function runs and
    its result is stored for next time.

    Args:
        func: The ADK @tool function to wrap (when used without parens).
        ttl: Cache TTL in seconds. None = use daemon default.

    Returns:
        Wrapped function with transparent caching.
    """
    if func is not None:
        # Used as @cached_tool without parentheses
        return _make_cached(func, ttl=ttl)

    # Used as @cached_tool(ttl=300) with parentheses
    def decorator(f):
        return _make_cached(f, ttl=ttl)
    return decorator


def _make_cached(func, *, ttl: int = None):
    """Internal: wrap a function with cache check + store."""
    tool_name = func.__name__
    is_async = asyncio.iscoroutinefunction(func)

    @functools.wraps(func)
    def sync_wrapper(*args, **kwargs):
        return _cached_call(func, tool_name, ttl, args, kwargs)

    @functools.wraps(func)
    async def async_wrapper(*args, **kwargs):
        return await _cached_call_async(func, tool_name, ttl, args, kwargs)

    return async_wrapper if is_async else sync_wrapper


def _cached_call(func, tool_name: str, ttl, args, kwargs):
    """Synchronous cache-check-execute cycle."""
    if not daemon_running():
        logger.debug("Daemon not running — bypassing cache for %s", tool_name)
        return func(*args, **kwargs)

    arguments = _build_args(args, kwargs)
    result = cached_mcp_check(ADAPTER_SERVER, tool_name, arguments, ttl=ttl)

    if result.get("cached"):
        logger.info("Cache HIT  adk/%s  —  %s", tool_name, _summarize_args(arguments))
        return result["data"]

    logger.info("Cache MISS adk/%s  —  executing live", tool_name)
    data = func(*args, **kwargs)
    _store_result(tool_name, arguments, data, ttl)
    return data


async def _cached_call_async(func, tool_name: str, ttl, args, kwargs):
    """Async cache-check-execute cycle."""
    if not daemon_running():
        logger.debug("Daemon not running — bypassing cache for %s", tool_name)
        return await func(*args, **kwargs)

    arguments = _build_args(args, kwargs)
    result = cached_mcp_check(ADAPTER_SERVER, tool_name, arguments, ttl=ttl)

    if result.get("cached"):
        logger.info("Cache HIT  adk/%s  —  %s", tool_name, _summarize_args(arguments))
        return result["data"]

    logger.info("Cache MISS adk/%s  —  executing live", tool_name)
    data = await func(*args, **kwargs)
    _store_result(tool_name, arguments, data, ttl)
    return data


def _store_result(tool_name: str, arguments: dict, data, ttl):
    """Store the result in ToolRecall cache.

    Serializes the result to JSON for storage. The cache stores
    the string representation — on retrieval it's deserialized back.
    """
    if not daemon_running():
        return
    import json
    try:
        serialized = json.dumps(data, default=str, ensure_ascii=False)
        key = cached_mcp_check(ADAPTER_SERVER, tool_name, arguments, ttl=ttl)
        cached_mcp_store(
            key.get("key", ""),
            ADAPTER_SERVER,
            tool_name,
            arguments,
            serialized,
            ttl=ttl,
        )
        logger.info("Cache STORE adk/%s  —  %d chars", tool_name, len(serialized))
    except (TypeError, ValueError) as e:
        logger.warning("Failed to serialize result for adk/%s: %s", tool_name, e)


def _build_args(args, kwargs) -> dict:
    """Build a clean arguments dict for cache key generation.

    Positional args are converted to keyword names where possible.
    The result is a JSON-serializable dict.
    """
    return {
        "_pos": [str(a) for a in args],
        **{
            k: str(v) if not isinstance(v, (str, int, float, bool, type(None))) else v
            for k, v in kwargs.items()
        },
    }


def _summarize_args(arguments: dict) -> str:
    """Short human-readable arg summary for log messages."""
    parts = []
    for k, v in list(arguments.items())[:3]:
        s = str(v)
        if len(s) > 40:
            s = s[:37] + "..."
        parts.append(f"{k}={s}")
    return ", ".join(parts)