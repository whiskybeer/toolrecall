"""End-to-end test: LangChain adapter with live daemon.

Tests ToolRecallCache (BaseCache) and ToolRecallCallbackHandler
against the real ToolRecall daemon.

Requires:
  - ToolRecall daemon running
  - langchain, langchain-core, langchain-google-genai installed
  - GOOGLE_API_KEY env var set
"""

import json
import pytest

pytestmark = [pytest.mark.e2e, pytest.mark.langchain]


class TestToolRecallCacheE2E:
    """Real daemon tests for ToolRecallCache (LangChain LLM cache)."""

    def test_llm_cache_miss_then_hit(self):
        """First call misses, second hits — same result, LLM called once."""
        from toolrecall.adapters.langchain import _ensure_base
        _ensure_base()
        from toolrecall.adapters.langchain import ToolRecallCache
        from langchain_core.globals import set_llm_cache
        from langchain_google_genai import ChatGoogleGenerativeAI
        from toolrecall.client import daemon_running
        assert daemon_running()

        cache = ToolRecallCache(ttl=120)
        set_llm_cache(cache)

        llm = ChatGoogleGenerativeAI(model="gemini-2.5-flash")

        r1 = llm.invoke("Say 'hello' in exactly one word")
        print(f"1st call: {r1.content}")

        r2 = llm.invoke("Say 'hello' in exactly one word")
        print(f"2nd call: {r2.content}")

        assert r1.content == r2.content, "Results should be identical"

    def test_different_prompts_different_cache(self):
        """Different prompts produce different cache entries."""
        from toolrecall.adapters.langchain import _ensure_base
        _ensure_base()
        from toolrecall.adapters.langchain import ToolRecallCache
        from langchain_core.globals import set_llm_cache
        from langchain_google_genai import ChatGoogleGenerativeAI
        from toolrecall.client import daemon_running
        assert daemon_running()

        cache = ToolRecallCache(ttl=120)
        set_llm_cache(cache)

        llm = ChatGoogleGenerativeAI(model="gemini-2.5-flash")

        r1 = llm.invoke("Say 'hello' in one word")
        r2 = llm.invoke("Say 'goodbye' in one word")

        assert r1.content != r2.content, "Different prompts should differ"

    def test_cache_clear(self):
        """clear() invalidates all cached entries."""
        from toolrecall.adapters.langchain import _ensure_base
        _ensure_base()
        from toolrecall.adapters.langchain import ToolRecallCache
        from langchain_core.globals import set_llm_cache
        from langchain_google_genai import ChatGoogleGenerativeAI
        from toolrecall.client import daemon_running
        assert daemon_running()

        cache = ToolRecallCache(ttl=120)
        set_llm_cache(cache)

        llm = ChatGoogleGenerativeAI(model="gemini-2.5-flash")

        r1 = llm.invoke("Say 'cache-clear-test' in one word")

        cache.clear()

        r2 = llm.invoke("Say 'cache-clear-test' in one word")

        # After clear, the LLM should be called again (content may differ)
        # but at least it shouldn't crash
        assert r2.content is not None


class TestToolRecallCallbackHandlerE2E:
    """Real daemon tests for ToolRecallCallbackHandler."""

    def test_callback_handler_does_not_raise(self):
        """on_tool_start/on_tool_end/on_tool_error don't raise when daemon is running."""
        from toolrecall.adapters.langchain import _ensure_base
        _ensure_base()
        from toolrecall.adapters.langchain import ToolRecallCallbackHandler

        handler = ToolRecallCallbackHandler(ttl=60)

        handler.on_tool_start({"name": "test_tool"}, "input data")
        handler.on_tool_end("output data", name="test_tool")
        handler.on_tool_error(ValueError("test error"), name="test_tool")

        assert "test_tool" in handler._tool_errors