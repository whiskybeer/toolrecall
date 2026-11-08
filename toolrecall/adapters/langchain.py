# Copyright (c) 2026 Robin Schultka
# SPDX-License-Identifier: MIT
# Source: https://github.com/whiskybeer/toolrecall
"""
LangChain Adapter — ToolRecallCache BaseCache subclass + cached_tool wrapper.

LangChain already has a BaseCache abstraction for LLM response caching.
This adapter implements ToolRecallCache as a BaseCache subclass for LLM
caching, plus cached_tool(), which wraps a LangChain BaseTool so repeat
calls with the same input are served from ToolRecall's cache.

Usage (LLM cache):
    from langchain.globals import set_llm_cache
    from toolrecall.adapters.langchain import ToolRecallCache

    set_llm_cache(ToolRecallCache())

    # Every LLM call now checks ToolRecall's local SQLite cache first.
    # Cache hit: returns instantly, zero tokens consumed.
    # Cache miss: calls the LLM, stores the result.

Usage (tool cache):
    from langchain_core.tools import tool
    from toolrecall.adapters.langchain import cached_tool

    @tool
    def search(query: str) -> str:
        '''Search the docs.'''
        ...

    search = cached_tool(search, ttl=300)

    # Same tool + same input -> cached result, the tool body does not run.
    # Input validation, callbacks and ToolMessage formatting still happen
    # as usual; only the tool's own _run/_arun is skipped on a hit.

    A LangChain callback handler cannot skip tool execution, so
    ToolRecallCallbackHandler is store-only: it records results keyed by
    tool name + input (the same key cached_tool reads), but it never serves
    a hit by itself. Use cached_tool to actually save tool calls.

Architecture:
    Both use toolrecall.cache functions (cached_mcp_check/store) through
    the ToolRecall daemon. The daemon manages the SQLite connection - the
    adapter never opens a direct DB connection, avoiding lock contention.

    The adapter "server" name is "langchain" - cache keys are namespaced
    so they don't collide with other frameworks.

Note:
    langchain_core is an optional dependency. Install it with:
        pip install langchain langchain-core
    The module imports without LangChain; _ensure_base() and cached_tool()
    raise ImportError when langchain_core is missing.
"""

import contextvars
import functools
import json
import logging
from typing import TYPE_CHECKING, Any, Optional

# langchain_core is optional - only imported when actually used
if TYPE_CHECKING:
    pass

from toolrecall.cache import cached_mcp_check, cached_mcp_store, invalidate_mcp_server
from toolrecall import client as _tr_client

logger = logging.getLogger("toolrecall.adapters.langchain")

ADAPTER_SERVER = "langchain"  # Cache namespace for LangChain


# ---- LLM Cache (BaseCache subclass) ------------------------


class ToolRecallCache:
    """LangChain BaseCache implementation backed by ToolRecall's SQLite cache.

    Caches LLM responses by prompt hash. Compatible with any LLM provider
    LangChain supports - OpenAI, Anthropic, Google, local models, etc.

    Key characteristics:
        - Persistent SQLite storage (survives process restarts)
        - TTL-based expiration (default: daemon's MCP default, ~3600s)
        - Daemon manages the DB - no lock contention
        - Cache keys are namespaced under "langchain" server

    Subclasses LangChain's BaseCache at import time. The class is defined
    here unconditionally but only used when langchain_core is installed.
    """

    def __init__(self, ttl: Optional[int] = None):
        self._ttl = ttl

    def _make_key(self, prompt: str, llm_string: str) -> str:
        """Generate a cache key from prompt + model identifier."""
        return f"llm:{prompt}:{llm_string}"

    def lookup(self, prompt: str, llm_string: str) -> Optional[list]:
        """Check if an LLM response is cached for this prompt.

        Returns cached generations on hit, None on miss.
        Deserializes the JSON-stored data back into ChatGeneration objects.
        """
        if not _tr_client.daemon_running():
            return None

        key = self._make_key(prompt, llm_string)
        result = cached_mcp_check(ADAPTER_SERVER, "llm_generate", {"key": key}, ttl=self._ttl)

        if result.get("cached"):
            logger.info("Cache HIT  langchain/llm  - key=%s...", key[:40])
            try:
                data = json.loads(result["data"])
                from langchain_core.outputs import ChatGeneration
                from langchain_core.messages import AIMessage

                generations = []
                for item in data:
                    msg = AIMessage(content=item.get("text", ""))
                    gen = ChatGeneration(
                        text=item.get("text", ""),
                        message=msg,
                        generation_info=item.get("generation_info"),
                    )
                    generations.append(gen)
                return generations
            except (json.JSONDecodeError, TypeError, KeyError) as e:
                logger.warning("Cache deserialization error: %s", e)
                return None

        return None

    def update(self, prompt: str, llm_string: str, return_val: list) -> None:
        """Store an LLM response in the ToolRecall cache.

        Serializes the generation list as JSON and stores it via
        the MCP cache store.
        """
        if not _tr_client.daemon_running():
            return

        key = self._make_key(prompt, llm_string)
        try:
            # Serialize ChatGeneration objects as dicts via model_dump
            serialized = json.dumps(
                [g.model_dump() if hasattr(g, "model_dump") else str(g) for g in return_val],
                ensure_ascii=False,
            )
            check_result = cached_mcp_check(
                ADAPTER_SERVER, "llm_generate", {"key": key}, ttl=self._ttl
            )
            cached_mcp_store(
                check_result.get("key", ""),
                ADAPTER_SERVER,
                "llm_generate",
                {"key": key},
                serialized,
                ttl=self._ttl,
            )
            logger.info("Cache STORE langchain/llm  - %d chars", len(serialized))
        except (TypeError, ValueError) as e:
            logger.warning("Failed to serialize LLM result: %s", e)

    def clear(self) -> None:
        """Clear all ToolRecall-cached LLM responses.

        This removes all entries under the "langchain" server namespace.
        """
        invalidate_mcp_server(ADAPTER_SERVER)
        logger.info("Cache CLEAR langchain - all entries invalidated")


# ---- Tool Cache (cached_tool wrapper) ----------------------
# Keys are derived from the tool INPUT (never its output) so the same tool +
# same input addresses the same row. String input -> {"input": s}; dict
# input -> {"kwargs": {...}} (key order canonicalized by cached_mcp_check's
# sort_keys). Values are stored JSON-encoded. Everything here is
# best-effort: a cache failure falls through to live execution.

# Callback plumbing BaseTool injects into _run - never part of a key. The
# RunnableConfig parameter is found per tool (its name is not fixed, and a
# real tool argument may be called "config").
_INJECTED_KWARGS = frozenset({"run_manager", "callbacks"})


def _injected_names(fn: Any) -> frozenset:
    """Names BaseTool injects into ``fn`` (run_manager, callbacks, RunnableConfig)."""
    try:
        from langchain_core.tools.base import _get_runnable_config_param

        config_param = _get_runnable_config_param(fn)
    except Exception:
        config_param = None
    return _INJECTED_KWARGS | ({config_param} if config_param else frozenset())


# Set while a cached _arun runs, so BaseTool's default _arun -> _run
# delegation doesn't check/store the same call a second time.
_IN_CACHED_CALL: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "toolrecall_langchain_in_cached_call", default=False
)


def _tool_arguments(args: tuple, kwargs: dict) -> Optional[dict]:
    """Canonical cache arguments for a tool input, or None if uncacheable.

    ``args``/``kwargs`` are what BaseTool passes to ``_run`` after input
    parsing, with injected plumbing already removed.
    """
    if len(args) == 1 and not kwargs and isinstance(args[0], str):
        arguments: dict = {"input": args[0]}
    elif args:
        arguments = {"args": list(args), "kwargs": kwargs}
    else:
        arguments = {"kwargs": kwargs}
    try:
        json.dumps(arguments, sort_keys=True)
    except (TypeError, ValueError):
        return None  # non-JSON input: no deterministic key -> run live
    return arguments


def _tool_lookup(name: str, arguments: dict, ttl: Optional[int]) -> tuple[bool, Any]:
    """Return (hit, value). Any cache error is a miss."""
    try:
        result = cached_mcp_check(ADAPTER_SERVER, name, arguments, ttl=ttl)
        if not result.get("cached"):
            return False, None
        return True, json.loads(result["data"])
    except Exception as e:  # cache must never break the tool
        logger.warning("Tool cache lookup failed for %s: %s", name, e)
        return False, None


def _tool_store(name: str, arguments: dict, value: Any, ttl: Optional[int]) -> None:
    """Store a successful tool result. Best-effort, never raises."""
    if ttl is not None and ttl <= 0:
        return  # ttl=0 means bypass: don't write either
    try:
        serialized = json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError) as e:
        logger.debug("Tool result for %s not JSON-serializable, not cached: %s", name, e)
        return
    try:
        check_result = cached_mcp_check(ADAPTER_SERVER, name, arguments, ttl=ttl)
        cached_mcp_store(
            check_result.get("key", ""),
            ADAPTER_SERVER,
            name,
            arguments,
            serialized,
            ttl=ttl,
        )
        logger.info("Cache STORE langchain/tool:%s - %d chars", name, len(serialized))
    except Exception as e:  # cache must never break the tool
        logger.warning("Failed to cache tool result for %s: %s", name, e)


def cached_tool(tool: Any, ttl: Optional[int] = None) -> Any:
    """Return a copy of a LangChain ``BaseTool`` that serves repeat calls from cache.

    The cache is keyed by tool name + parsed tool input. On a hit the tool's
    ``_run``/``_arun`` is skipped and the cached value is returned; input
    validation, callbacks (on_tool_start/on_tool_end) and ToolMessage
    formatting still run as usual. On a miss the tool runs and a successful,
    JSON-serializable result is stored. Exceptions are never cached.

    Results round-trip through JSON (tuples become lists, except the
    ``(content, artifact)`` pair of ``response_format="content_and_artifact"``
    tools, which is restored). Daemon down or any cache error -> the tool
    runs live, exactly as unwrapped.

    Args:
        tool: A ``langchain_core.tools.BaseTool`` instance (e.g. from ``@tool``).
        ttl: Cache TTL in seconds. None = daemon default, 0 = bypass.

    Returns:
        A wrapped copy of ``tool``; the original instance is not modified.
    """
    try:
        from langchain_core.tools import BaseTool
    except ImportError as e:
        raise ImportError(
            "langchain_core is required for cached_tool. "
            "Install it with: pip install langchain-core"
        ) from e

    if not isinstance(tool, BaseTool):
        raise TypeError(f"cached_tool expects a LangChain BaseTool, got {type(tool).__name__}")

    wrapped = tool.model_copy()
    name = wrapped.name
    restore_tuple = getattr(wrapped, "response_format", "content") == "content_and_artifact"
    filter_injected = getattr(wrapped, "_filter_injected_args", None)

    def _key(args: tuple, kwargs: dict, injected: frozenset) -> Optional[dict]:
        if not _tr_client.daemon_running():
            return None
        clean = {k: v for k, v in kwargs.items() if k not in injected}
        if callable(filter_injected):
            try:
                clean = filter_injected(clean)  # drops InjectedToolArg params
            except Exception:
                pass
        return _tool_arguments(args, clean)

    def _decode(value: Any) -> Any:
        if restore_tuple and isinstance(value, list) and len(value) == 2:
            return tuple(value)
        return value

    orig_run = wrapped._run
    run_injected = _injected_names(orig_run)

    # functools.wraps keeps the original signature visible, so BaseTool
    # still injects run_manager / config exactly as for the unwrapped tool.
    @functools.wraps(orig_run)
    def _run(*args: Any, **kwargs: Any) -> Any:
        if _IN_CACHED_CALL.get():
            return orig_run(*args, **kwargs)
        arguments = _key(args, kwargs, run_injected)
        if arguments is None:
            return orig_run(*args, **kwargs)
        hit, value = _tool_lookup(name, arguments, ttl)
        if hit:
            logger.info("Cache HIT   langchain/tool:%s", name)
            return _decode(value)
        value = orig_run(*args, **kwargs)
        _tool_store(name, arguments, value, ttl)
        return value

    object.__setattr__(wrapped, "_run", _run)

    # Only wrap _arun when the class overrides it; BaseTool's default _arun
    # delegates to self._run, which is already wrapped above.
    if type(wrapped)._arun is not BaseTool._arun:
        orig_arun = wrapped._arun
        arun_injected = _injected_names(orig_arun)

        @functools.wraps(orig_arun)
        async def _arun(*args: Any, **kwargs: Any) -> Any:
            arguments = _key(args, kwargs, arun_injected)
            if arguments is None:
                return await orig_arun(*args, **kwargs)
            hit, value = _tool_lookup(name, arguments, ttl)
            if hit:
                logger.info("Cache HIT   langchain/tool:%s", name)
                return _decode(value)
            token = _IN_CACHED_CALL.set(True)
            try:
                value = await orig_arun(*args, **kwargs)
            finally:
                _IN_CACHED_CALL.reset(token)
            _tool_store(name, arguments, value, ttl)
            return value

        object.__setattr__(wrapped, "_arun", _arun)

    return wrapped


# ---- Tool result recorder (Callback Handler, store-only) ---


class ToolRecallCallbackHandler:
    """LangChain callback handler that records tool results (store-only).

    A callback handler cannot skip tool execution, so this handler never
    saves a tool call by itself. It stores each successful string result
    keyed by tool name + tool INPUT (captured in on_tool_start per run_id),
    using the same key as cached_tool() - so a later cached_tool-wrapped
    call with the same input can be served from what was recorded here.
    Non-string outputs (e.g. ToolMessage when called with a tool_call_id)
    are not recorded. Use cached_tool() to actually serve cache hits.
    """

    def __init__(self, ttl: Optional[int] = None):
        self._ttl = ttl
        self._tool_errors: set[str] = set()
        self._pending: dict[Any, tuple[str, dict]] = {}

    def on_tool_start(
        self,
        serialized: dict[str, Any],
        input_str: str,
        **kwargs: Any,
    ) -> None:
        """Called before a tool runs. Remembers the input for on_tool_end."""
        name = serialized.get("name", "unknown")
        self._tool_errors.discard(name)
        inputs = kwargs.get("inputs")
        if isinstance(inputs, dict):
            arguments = _tool_arguments((), dict(inputs))
        else:
            arguments = _tool_arguments((input_str,), {})
        if arguments is not None:
            self._pending[kwargs.get("run_id")] = (name, arguments)

    def on_tool_end(
        self,
        output: Any,
        observation_prefix: Optional[str] = None,
        llm_prefix: Optional[str] = None,
        **kwargs: Any,
    ) -> None:
        """Called after a tool completes. Stores the result keyed by its input.

        Best-effort: failures are logged, never raised.
        """
        pending = self._pending.pop(kwargs.get("run_id"), None)
        if pending is None or not isinstance(output, str):
            return
        if not _tr_client.daemon_running():
            return
        name, arguments = pending
        _tool_store(name, arguments, output, self._ttl)

    def on_tool_error(
        self,
        error: BaseException,
        **kwargs: Any,
    ) -> None:
        """Called when a tool raises an error. We don't cache errors."""
        self._pending.pop(kwargs.get("run_id"), None)
        name = kwargs.get("name", "unknown")
        self._tool_errors.add(name)
        logger.debug("Tool error (not cached): %s - %s", name, error)


# ---- Lazy base class binding --------------------------------
# We define the class above without inheriting from BaseCache so the
# module can be imported without langchain_core. If you want to use
# it as a proper BaseCache subclass, call ensure_base() first.

_BaseCache = None
_BaseCallbackHandler = None


def _ensure_base():
    """Lazy-import langchain_core base classes.

    This is called at first use so the module is importable without
    langchain being installed. If langchain is not installed, raises
    ImportError with a clear message.
    """
    global _BaseCache, _BaseCallbackHandler, ToolRecallCache, ToolRecallCallbackHandler

    if _BaseCache is not None:
        return

    try:
        from langchain_core.caches import BaseCache as BC
        from langchain_core.callbacks import BaseCallbackHandler as BCH

        _BaseCache = BC
        _BaseCallbackHandler = BCH

        # Save references to the originals before replacing them
        _OriginalCache = ToolRecallCache
        _OriginalHandler = ToolRecallCallbackHandler

        # Dynamically inherit from the BaseCache class
        class ToolRecallCacheImpl(BC):  # type: ignore
            __doc__ = ToolRecallCache.__doc__

            def __init__(self, ttl: Optional[int] = None):
                super().__init__()
                self._impl = _OriginalCache(ttl=ttl)
                self._ttl = ttl

            def _make_key(self, prompt: str, llm_string: str) -> str:
                return self._impl._make_key(prompt, llm_string)

            def lookup(self, prompt: str, llm_string: str) -> Optional[list]:
                return self._impl.lookup(prompt, llm_string)

            def update(self, prompt: str, llm_string: str, return_val: list) -> None:
                self._impl.update(prompt, llm_string, return_val)

            def clear(self) -> None:
                self._impl.clear()

        ToolRecallCache = ToolRecallCacheImpl  # type: ignore

        # Dynamically inherit from BaseCallbackHandler
        class ToolRecallCallbackHandlerImpl(BCH):  # type: ignore
            __doc__ = ToolRecallCallbackHandler.__doc__

            def __init__(self, ttl: Optional[int] = None):
                super().__init__()
                self._impl = _OriginalHandler(ttl=ttl)
                self._tool_errors = self._impl._tool_errors
                self._pending = self._impl._pending
                self._ttl = self._impl._ttl

            def on_tool_start(self, serialized, input_str, **kwargs):
                self._impl.on_tool_start(serialized, input_str, **kwargs)

            def on_tool_end(self, output, observation_prefix=None, llm_prefix=None, **kwargs):
                self._impl.on_tool_end(output, observation_prefix, llm_prefix, **kwargs)

            def on_tool_error(self, error, **kwargs):
                self._impl.on_tool_error(error, **kwargs)

        ToolRecallCallbackHandler = ToolRecallCallbackHandlerImpl  # type: ignore

    except ImportError as e:
        raise ImportError(
            "langchain_core is required for ToolRecallCache. "
            "Install it with: pip install langchain-core"
        ) from e
