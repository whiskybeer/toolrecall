"""
Tests for ToolRecall Framework Adapters.

Tests each adapter module for import correctness, basic cache-hit/miss
logic, and graceful fallback when the daemon is not running.

Since the adapters depend on a running daemon for full integration,
these tests focus on:
1. Module imports work (no ImportError for optional deps)
2. The daemon-running check correctly bypasses cache when daemon is down
3. Cache key generation is deterministic
4. Error handling doesn't raise
"""

import sys
import pytest


# ---- Fixtures ------------------------------------------------


@pytest.fixture(autouse=True)
def _ensure_daemon_not_running():
    """Ensure no daemon is running during unit tests.

    Tests that need a real daemon use the e2e test marker.
    We mock daemon_running() to return False by default so
    adapters gracefully fall through to direct execution.
    """
    import toolrecall.client
    original = toolrecall.client.daemon_running
    toolrecall.client.daemon_running = lambda: False
    yield
    toolrecall.client.daemon_running = original


# ---- Google ADK Adapter Tests --------------------------------


class TestGoogleADKAdapter:
    """Test the @cached_tool decorator."""

    def test_import(self):
        """Module imports without error."""
        from toolrecall.adapters import google_adk
        assert google_adk is not None
        assert hasattr(google_adk, "cached_tool")

    def test_decorator_no_args(self):
        """@cached_tool without parentheses works."""
        from toolrecall.adapters.google_adk import cached_tool

        @cached_tool
        def my_tool(query: str) -> str:
            return f"result for {query}"

        # Daemon is not running, so this should fall through to direct exec
        result = my_tool(query="hello")
        assert result == "result for hello"

    def test_decorator_with_args(self):
        """@cached_tool(ttl=300) with parentheses works."""
        from toolrecall.adapters.google_adk import cached_tool

        @cached_tool(ttl=300)
        def my_tool(query: str) -> str:
            return f"result for {query}"

        result = my_tool(query="world")
        assert result == "result for world"

    def test_async_function(self):
        """@cached_tool wraps async functions correctly."""
        from toolrecall.adapters.google_adk import cached_tool

        @cached_tool
        async def async_tool(query: str) -> str:
            return f"async result for {query}"

        import asyncio
        result = asyncio.run(async_tool(query="test"))
        assert result == "async result for test"

    def test_multiple_positional_args(self):
        """@cached_tool works with positional args."""
        from toolrecall.adapters.google_adk import cached_tool

        @cached_tool
        def add(a: int, b: int) -> int:
            return a + b

        result = add(3, 4)
        assert result == 7

    def test_build_args_keyword(self):
        """_build_args returns correct kwargs dict."""
        from toolrecall.adapters.google_adk import _build_args

        args = _build_args((), {"query": "hello", "limit": 10})
        assert args["query"] == "hello"
        assert args["limit"] == 10

    def test_build_args_positional(self):
        """_build_args captures positional args."""
        from toolrecall.adapters.google_adk import _build_args

        args = _build_args(("hello", 10), {})
        assert args["_pos"] == ["hello", "10"]

    def test_decorator_preserves_metadata(self):
        """@cached_tool preserves function name and docstring."""
        from toolrecall.adapters.google_adk import cached_tool

        @cached_tool
        def search_web(query: str) -> str:
            """Search the web."""
            return f"result: {query}"

        assert search_web.__name__ == "search_web"
        assert search_web.__doc__ == "Search the web."

    def test_ttl_none_falls_through(self):
        """ttl=None should not raise (default handled by cache module)."""
        from toolrecall.adapters.google_adk import cached_tool

        @cached_tool(ttl=None)
        def my_tool(x: int) -> int:
            return x * 2

        result = my_tool(5)
        assert result == 10

    def test_side_effect_preserved(self):
        """Running the function still produces side effects (daemon not running = fallthrough)."""
        from toolrecall.adapters.google_adk import cached_tool

        side_effects = []

        @cached_tool
        def append_tool(item: str) -> str:
            side_effects.append(item)
            return f"appended {item}"

        result = append_tool(item="test")
        assert result == "appended test"
        assert "test" in side_effects


# ---- LangChain Adapter Tests ---------------------------------


class TestLangChainAdapter:
    """Test the LangChain adapter (ToolRecallCache + ToolRecallCallbackHandler).

    These tests verify the module structure and fallback behavior.
    Since langchain_core is not installed in CI, the dynamic base class
    binding path is tested separately.
    """

    def test_import_without_langchain(self):
        """Module imports without error even without langchain_core."""
        import toolrecall.adapters.langchain as lc_adapter

        assert lc_adapter is not None
        assert hasattr(lc_adapter, "ToolRecallCache")
        assert hasattr(lc_adapter, "ToolRecallCallbackHandler")

    def test_ensure_base_succeeds_with_langchain(self):
        """_ensure_base succeeds when langchain_core is installed."""
        from toolrecall.adapters.langchain import _ensure_base

        _ensure_base()
        # Should not raise — langchain is installed

    def test_tool_recall_cache_basic_usage(self):
        """ToolRecallCache works as standalone without BaseCache inheritance."""
        from toolrecall.adapters.langchain import ToolRecallCache

        cache = ToolRecallCache(ttl=300)
        assert cache._ttl == 300

        # Daemon not running -> lookup returns None, update does nothing
        result = cache.lookup("test prompt", "gpt-4")
        assert result is None

        # update should not raise
        cache.update("test prompt", "gpt-4", [{"text": "hello"}])
        clear_result = cache.clear()
        assert clear_result is None

    def test_tool_recall_cache_clear(self):
        """clear() does not raise when daemon is not running."""
        from toolrecall.adapters.langchain import ToolRecallCache

        cache = ToolRecallCache()
        # Should not raise
        cache.clear()

    def test_callback_handler_basic_usage(self):
        """ToolRecallCallbackHandler works without BaseCallbackHandler inheritance."""
        from toolrecall.adapters.langchain import ToolRecallCallbackHandler

        handler = ToolRecallCallbackHandler(ttl=300)
        assert handler._ttl == 300

        # Daemon not running -> all callbacks are no-ops
        handler.on_tool_start({"name": "test_tool"}, "input data")
        handler.on_tool_end("output data", name="test_tool")
        # Should not raise
        handler.on_tool_error(ValueError("test error"), name="test_tool")
        assert "test_tool" in handler._tool_errors

    def test_make_key_deterministic(self):
        """Cache keys are deterministic for same prompt + model."""
        from toolrecall.adapters.langchain import ToolRecallCache

        cache = ToolRecallCache()
        key1 = cache._make_key("hello world", "gpt-4")
        key2 = cache._make_key("hello world", "gpt-4")
        assert key1 == key2

    def test_make_key_different_prompt_different_key(self):
        """Different prompts produce different cache keys."""
        from toolrecall.adapters.langchain import ToolRecallCache

        cache = ToolRecallCache()
        key1 = cache._make_key("hello", "gpt-4")
        key2 = cache._make_key("world", "gpt-4")
        assert key1 != key2

    def test_make_key_different_model_different_key(self):
        """Different models produce different cache keys."""
        from toolrecall.adapters.langchain import ToolRecallCache

        cache = ToolRecallCache()
        key1 = cache._make_key("hello", "gpt-4")
        key2 = cache._make_key("hello", "claude-3")
        assert key1 != key2

    def test_callback_handler_multiple_tools(self):
        """Callback handler tracks errors per tool."""
        from toolrecall.adapters.langchain import ToolRecallCallbackHandler

        handler = ToolRecallCallbackHandler()

        handler.on_tool_start({"name": "tool_a"}, "input")
        handler.on_tool_error(ValueError("err"), name="tool_a")

        handler.on_tool_start({"name": "tool_b"}, "input")
        handler.on_tool_end("success", name="tool_b")

        assert "tool_a" in handler._tool_errors
        assert "tool_b" not in handler._tool_errors


# ---- Herdr Adapter Tests ------------------------------------


class TestHerdrAdapter:
    """Test the herdr integration guide module."""

    def test_import(self):
        """Module imports without error."""
        import toolrecall.adapters.herdr
        assert toolrecall.adapters.herdr is not None

    def test_setup_notice(self):
        """setup_notice() prints without error."""
        from toolrecall.adapters.herdr import setup_notice
        import io
        captured = io.StringIO()
        sys.stdout = captured
        setup_notice()
        sys.stdout = sys.__stdout__
        output = captured.getvalue()
        assert "ToolRecall" in output
        assert "herdr" in output
        assert "tr" in output


# ---- Adapter Package Tests ----------------------------------


class TestAdapterPackage:
    """Test the adapters package __init__."""

    def test_import_all(self):
        """All adapters are importable from the package."""
        from toolrecall.adapters import google_adk, langchain, herdr
        assert google_adk is not None
        assert langchain is not None
        assert herdr is not None

    def test_package_has_all(self):
        """__all__ lists all adapters."""
        import toolrecall.adapters
        assert "google_adk" in toolrecall.adapters.__all__
        assert "langchain" in toolrecall.adapters.__all__
        assert "herdr" in toolrecall.adapters.__all__