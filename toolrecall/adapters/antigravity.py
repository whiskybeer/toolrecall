# Copyright (c) 2026 Robin Schultka
# SPDX-License-Identifier: MIT
# Source: https://github.com/whiskybeer/toolrecall
"""
Google Antigravity Adapter — cached_tool decorator for Antigravity SDK tools.

Requires: google-antigravity (optional — only for ToolContext exclusion; the
decorator itself is framework-free and works on any callable).

Usage (Antigravity SDK — tools are plain callables passed to the Agent):

    from toolrecall.adapters import antigravity

    @antigravity.cached_tool(ttl=300)
    async def search_web(query: str) -> str:
        # Runs only on cache miss
        return await external_api_call(query)

    config = LocalAgentConfig(tools=[search_web])   # plain callable — no wrapper

Usage (any Python callable, SDK not required):

    @antigravity.cached_tool(ttl=60)
    def fetch(url: str) -> str:
        return urlopen(url).read().decode()

Architecture:
    Wraps toolrecall.cache.cached_mcp_check / cached_mcp_store. The adapter
    server name is "antigravity" — cache keys are namespaced so they don't
    collide with other frameworks.

    Request hashes are derived in the adapter via toolrecall._db._hash using
    the same formula cache.py applies (``{server}://{tool}?{key}``) instead of
    reusing the key returned by cached_mcp_check — that key is only present on
    the miss path, so trusting it stores under a hash of the empty string
    (the bug the ADK adapter shipped with; see langgraph._request_hash).

    ToolContext exclusion: the Antigravity SDK injects a stateful
    ToolContext into tools whose signature declares one (tool_runner.py
    _inject_context). The context is conversation state, never a
    deterministic input — it is excluded from cache keys via signature
    inspection mirroring the SDK's own _find_context_param. If the SDK is
    not installed, the exclusion degrades to an annotation-name check
    ("ToolContext") which covers the documented usage.

    Cache hits return the JSON-roundtripped result (dict/list/str); rich
    objects (pydantic models, dataclasses) come back stringified. The model
    consumes stringified tool results anyway — same trade the ADK adapter
    makes. Errors are never cached: an exception propagates live, nothing
    is stored.

    Graceful bypass: daemon not running → the function executes live, no
    crash, nothing stored. ttl=0 → never check, never store (opt-out per
    tool).
"""

import functools
import inspect
import json
import logging

from toolrecall import client as _tr_client
from toolrecall.cache import cached_mcp_check, cached_mcp_store

logger = logging.getLogger("toolrecall.adapters.antigravity")

ADAPTER_SERVER = "antigravity"  # Cache namespace for Antigravity tool calls


def _request_hash(tool: str, args_json: str) -> str:
    """Fallback key derivation matching cache.py's plain-JSON formula.

    Primary path: cached_mcp_check returns its own derived ``key`` on the
    miss path — the store reuses it, so check and store share one hash
    computation (including ``[norm]`` normalization, which this fallback
    cannot reproduce). The fallback only fires if a future check version
    omits the key.
    """
    from toolrecall._db import _hash

    return _hash(f"{ADAPTER_SERVER}://{tool}?{args_json}")


def _context_param_name(func) -> str | None:
    """Name of the ToolContext-typed parameter, if the signature declares one.

    Mirrors the SDK's tool_runner._find_context_param: resolve type hints and
    look for the ToolContext annotation. Falls back to an annotation-name
    check when the SDK is not installed (unresolvable forward reference).
    """
    try:
        import typing

        hints = dict(typing.get_type_hints(func))
    except Exception:  # noqa: BLE001 — unresolvable forward refs: use raw annotations
        hints = dict(getattr(func, "__annotations__", {}) or {})
    try:
        from google.antigravity.tools.tool_context import ToolContext
    except ImportError:
        ToolContext = None
    for name, ann in hints.items():
        if name == "return":
            continue
        if _annotation_mentions(ann, "ToolContext"):
            return name
        if ToolContext is not None and _origin_args_contain(ann, ToolContext):
            return name
    return None


def _origin_args_contain(ann, target) -> bool:
    """True if target appears inside Optional[X] / Union[...] / Annotated[...]."""
    import typing

    for arg in getattr(ann, "__args__", ()) or ():
        if arg is target:
            return True
        if _annotation_mentions(arg, getattr(target, "__name__", "")):
            return True
    if typing.get_origin(ann) is None and ann is target:
        return True
    return False


def _annotation_mentions(ann, name: str) -> bool:
    """Fallback without the SDK: does the annotation string mention the name?"""
    if not name:
        return False
    return name in getattr(ann, "__name__", "") or name in str(ann)


def cached_tool(func=None, *, ttl: int | None = None, freshness: bool = False):
    """Cache Antigravity SDK tool results via ToolRecall.

    Use with or without arguments:
        @cached_tool              # default TTL (daemon config)
        @cached_tool(ttl=300)     # custom TTL in seconds; ttl=0 → opt-out
        @cached_tool(freshness=True)  # return (result, metadata) tuples

    freshness=True: the wrapped tool returns ``(result, meta)`` where
    ``meta = {"cached": bool, "age_s": float|None}`` — age is the row's
    seconds-since-store on a cache hit (None on a live call). The agent
    can then reason about staleness ("this is 40 min old, refetch before
    quoting"). ToolContext parameters (SDK-injected conversation state)
    are excluded from the cache key automatically. Works on sync and
    async functions.
    """
    if func is not None:
        return _make_cached(func, ttl=ttl, freshness=False)
    return lambda f: _make_cached(f, ttl=ttl, freshness=freshness)


def _make_cached(func, *, ttl: int | None, freshness: bool = False):
    tool_name = getattr(func, "__name__", None) or type(func).__name__
    ctx_param = _context_param_name(func)
    is_async = _is_async(func)

    @functools.wraps(func)
    def sync_wrapper(*args, **kwargs):
        return _call(func, tool_name, ttl, ctx_param, args, kwargs, freshness=freshness)

    @functools.wraps(func)
    async def async_wrapper(*args, **kwargs):
        return await _call_async(func, tool_name, ttl, ctx_param, args, kwargs, freshness=freshness)

    return async_wrapper if is_async else sync_wrapper


def _is_async(fn) -> bool:
    """Mirror tool_runner._is_async: coroutine function or async __call__."""
    if inspect.iscoroutinefunction(fn):
        return True
    call = getattr(fn, "__call__", None)
    return call is not None and inspect.iscoroutinefunction(call)


def _key_arguments(func, args: tuple, kwargs: dict, ctx_param: str | None) -> dict:
    """Deterministic key inputs: positional args + kwargs, minus ToolContext.

    The SDK's runner invokes tools with kwargs only, but direct calls may use
    positionals — signature binding canonicalizes both into one dict so they
    collide into the same cache entry. Args that don't bind to a signature
    parameter (e.g. **kwargs-style extras the runner passes through) are
    retained verbatim. The ToolContext parameter is conversation state,
    never a cache-key input.
    """
    arguments: dict = {}
    sig = None
    try:
        sig = inspect.signature(func)
    except (TypeError, ValueError):
        sig = None
    bound = None
    if sig is not None:
        try:
            bound = sig.bind_partial(*args, **kwargs)
            bound.apply_defaults()
            arguments.update(
                {
                    k: v if isinstance(v, (str, int, float, bool, type(None))) else str(v)
                    for k, v in bound.arguments.items()
                    if k != ctx_param
                }
            )
        except TypeError:
            bound = None
    if bound is None:
        # Fallback: no clean binding (raw *args/**kwargs functions etc.)
        arguments.update(
            {
                "_pos": [str(a) for a in args],
                **{
                    k: v if isinstance(v, (str, int, float, bool, type(None))) else str(v)
                    for k, v in kwargs.items()
                    if k != ctx_param
                },
            }
        )
    if ctx_param is not None:
        arguments["_ctx_excluded"] = ctx_param
    return arguments


def _call(func, tool_name, ttl, ctx_param, args, kwargs, freshness: bool = False):
    if ttl == 0:
        data = func(*args, **kwargs)
        return (data, {"cached": False, "age_s": None}) if freshness else data
    if not _tr_client.daemon_running():
        logger.debug("Daemon not running — bypassing cache for %s", tool_name)
        data = func(*args, **kwargs)
        return (data, {"cached": False, "age_s": None}) if freshness else data
    arguments = _key_arguments(func, args, kwargs, ctx_param)
    result = cached_mcp_check(ADAPTER_SERVER, tool_name, arguments, ttl=ttl)
    if result.get("cached"):
        logger.info("Cache HIT  antigravity/%s", tool_name)
        data = json.loads(result["data"])
        if freshness:
            return (data, {"cached": True, "age_s": result.get("age_s")})
        return data
    data = func(*args, **kwargs)
    _store(tool_name, arguments, data, ttl, request_hash=result.get("key"))
    return (data, {"cached": False, "age_s": None}) if freshness else data


async def _call_async(func, tool_name, ttl, ctx_param, args, kwargs, freshness: bool = False):
    if ttl == 0:
        data = await func(*args, **kwargs)
        return (data, {"cached": False, "age_s": None}) if freshness else data
    if not _tr_client.daemon_running():
        logger.debug("Daemon not running — bypassing cache for %s", tool_name)
        data = await func(*args, **kwargs)
        return (data, {"cached": False, "age_s": None}) if freshness else data
    arguments = _key_arguments(func, args, kwargs, ctx_param)
    result = cached_mcp_check(ADAPTER_SERVER, tool_name, arguments, ttl=ttl)
    if result.get("cached"):
        logger.info("Cache HIT  antigravity/%s", tool_name)
        data = json.loads(result["data"])
        if freshness:
            return (data, {"cached": True, "age_s": result.get("age_s")})
        return data
    data = await func(*args, **kwargs)
    _store(tool_name, arguments, data, ttl, request_hash=result.get("key"))
    return (data, {"cached": False, "age_s": None}) if freshness else data


def _store(
    tool_name: str, arguments: dict, data, ttl: int | None, request_hash: str | None = None
) -> None:
    """Store under the check-derived request hash. Never caches errors.

    ``request_hash`` comes from the miss-path check result (single-sourced
    with check's key derivation, including ``[norm]`` normalization). Only
    falls back to plain-JSON re-derivation if the check returned no key.
    """
    try:
        if not _tr_client.daemon_running():
            return
        serialized = json.dumps(data, default=str, ensure_ascii=False)
        if not request_hash:
            args_json = json.dumps(arguments, sort_keys=True)
            request_hash = _request_hash(tool_name, args_json)
        cached_mcp_store(
            request_hash,
            ADAPTER_SERVER,
            tool_name,
            arguments,
            serialized,
            ttl=ttl,
        )
        logger.info("Cache STORE antigravity/%s — %d chars", tool_name, len(serialized))
    except (TypeError, ValueError) as e:
        logger.warning("antigravity/%s store failed: %s", tool_name, e)
