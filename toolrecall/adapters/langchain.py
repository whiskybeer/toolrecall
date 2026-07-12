"""
LangChain Adapter — ToolRecallCache BaseCache subclass + auto callback handler.

LangChain already has a BaseCache abstraction for LLM response caching, and
a callback system for intercepting tool execution results. This adapter
implements ToolRecallCache as a BaseCache subclass for LLM caching, plus
a ToolRecallCallbackHandler that intercepts tool execution results.

Usage (LLM cache):
    from langchain.globals import set_llm_cache
    from toolrecall.adapters.langchain import ToolRecallCache

    set_llm_cache(ToolRecallCache())

    # Every LLM call now checks ToolRecall's local SQLite cache first.
    # Cache hit: returns instantly, zero tokens consumed.
    # Cache miss: calls the LLM, stores the result.

Usage (tool cache):
    from langchain.callbacks.base import BaseCallbackManager
    from toolrecall.adapters.langchain import ToolRecallCallbackHandler

    callback = ToolRecallCallbackHandler()
    manager = BaseCallbackManager.add_handler(callback)

    # Tool execution results are cached under their tool name + args hash.
    # Same tool + same args -> cached result from ToolRecall's MCP cache.

Architecture:
    Both use toolrecall.cache functions (cached_mcp_check/store) through
    the ToolRecall daemon. The daemon manages the SQLite connection - the
    adapter never opens a direct DB connection, avoiding lock contention.

    The adapter "server" name is "langchain" - cache keys are namespaced
    so they don't collide with other frameworks.

Note:
    langchain_core is an optional dependency. Install with:
        pip install toolrecall[langchain]
    or:
        pip install langchain langchain-core
    Import the adapter only when LangChain is installed - the module
    will raise ImportError otherwise.
"""

import json
import logging
from typing import TYPE_CHECKING, Any, Optional

# langchain_core is optional - only imported when actually used
if TYPE_CHECKING:
    pass

from toolrecall.cache import cached_mcp_check, cached_mcp_store, invalidate_mcp_server
from toolrecall.client import daemon_running

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
        if not daemon_running():
            return None

        key = self._make_key(prompt, llm_string)
        result = cached_mcp_check(
            ADAPTER_SERVER, "llm_generate", {"key": key}, ttl=self._ttl
        )

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
        if not daemon_running():
            return

        key = self._make_key(prompt, llm_string)
        try:
            # Serialize ChatGeneration objects as dicts via model_dump
            serialized = json.dumps(
                [g.model_dump() if hasattr(g, 'model_dump') else str(g) for g in return_val],
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


# ---- Tool Cache (Callback Handler) -------------------------


class ToolRecallCallbackHandler:
    """LangChain callback handler that caches tool execution results.

    Intercepts tool start/finish events and stores the result keyed by
    tool name + arguments. Repeat calls with identical args hit the local
    cache instead of re-executing the tool.

    This is a lightweight handler - it does NOT modify the tool execution
    pipeline itself. For that, combine with ToolRecallCache or the MCP
    multiplexer which provides native-named tools with transparent caching.
    """

    def __init__(self, ttl: Optional[int] = None):
        self._ttl = ttl
        self._tool_errors: set[str] = set()

    def on_tool_start(
        self,
        serialized: dict[str, Any],
        input_str: str,
        **kwargs: Any,
    ) -> None:
        """Called before a tool runs. Resets error tracking."""
        self._tool_errors.discard(serialized.get("name", "unknown"))

    def on_tool_end(
        self,
        output: str,
        observation_prefix: Optional[str] = None,
        llm_prefix: Optional[str] = None,
        **kwargs: Any,
    ) -> None:
        """Called after a tool completes. Stores result in ToolRecall cache.

        The result is stored under the tool's name + input string hash.
        This is a best-effort cache - failures are logged but not raised.
        """
        if not daemon_running():
            return

        name = kwargs.get("name", "unknown")
        arguments = {"input": str(output)}

        try:
            check_result = cached_mcp_check(
                ADAPTER_SERVER, name, arguments, ttl=self._ttl
            )
            cached_mcp_store(
                check_result.get("key", ""),
                ADAPTER_SERVER,
                name,
                arguments,
                str(output),
                ttl=self._ttl,
            )
            logger.info(
                "Cache STORE langchain/tool:%s  -  %d chars", name, len(str(output))
            )
        except Exception as e:
            logger.warning("Failed to cache tool result for %s: %s", name, e)

    def on_tool_error(
        self,
        error: BaseException,
        **kwargs: Any,
    ) -> None:
        """Called when a tool raises an error. We don't cache errors."""
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
            "Install it with: pip install toolrecall[langchain]"
        ) from e