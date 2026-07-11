"""End-to-end test: Google ADK adapter with live daemon.

Tests the @cached_tool decorator against the real ToolRecall daemon,
including cache-hit/miss cycle, FunctionTool integration, and all
common return types (str, dict, list, int, None, bool).

Requires:
  - ToolRecall daemon running (toolrecall daemon)
  - google-adk package installed
"""

import json
import pytest


# ---- cached_tool decorator tests ----

class TestCachedToolE2E:
    """Real daemon tests for the @cached_tool decorator."""

    def test_cache_miss_then_hit(self):
        """First call misses, second call hits — same result, function runs once."""
        from toolrecall.adapters.google_adk import cached_tool

        call_count = [0]

        @cached_tool(ttl=60)
        def search(query: str) -> str:
            call_count[0] += 1
            return f"Results for {query}"

        r1 = search(query="e2e-test")
        r2 = search(query="e2e-test")

        assert r1 == r2, "Results should be identical"
        assert call_count[0] == 1, "Function should run only once (second call cached)"
        assert isinstance(r1, str)

    def test_cached_tool_with_function_tool(self):
        """@cached_tool works with ADK's FunctionTool decorator."""
        from toolrecall.adapters.google_adk import cached_tool
        from google.adk.tools.function_tool import FunctionTool

        call_count = [0]

        @FunctionTool
        @cached_tool(ttl=60)
        def get_status(service: str) -> str:
            call_count[0] += 1
            return f"{service}: healthy"

        # FunctionTool wraps the inner function — call it's func attribute
        r1 = get_status.func(service="api")
        r2 = get_status.func(service="api")

        assert r1 == r2
        assert call_count[0] == 1
        assert "api: healthy" in str(r1)

    def test_different_args_different_cache(self):
        """Different arguments produce different cache keys."""
        from toolrecall.adapters.google_adk import cached_tool

        call_count = [0]

        @cached_tool(ttl=60)
        def lookup(id: int) -> str:
            call_count[0] += 1
            return f"record-{id}"

        r1 = lookup(id=1)
        r2 = lookup(id=2)

        assert r1 == "record-1"
        assert r2 == "record-2"
        assert call_count[0] == 2, "Different args = different cache keys"

    def test_dict_return_type(self):
        """Tools returning dicts are cached and restored correctly."""
        from toolrecall.adapters.google_adk import cached_tool

        call_count = [0]

        @cached_tool(ttl=60)
        def get_config(env: str) -> dict:
            call_count[0] += 1
            return {"env": env, "port": 8080, "debug": False}

        r1 = get_config(env="staging")
        r2 = get_config(env="staging")

        assert r1 == r2
        assert call_count[0] == 1
        assert r1["env"] == "staging"
        assert r1["port"] == 8080

    def test_list_return_type(self):
        """Tools returning lists are cached and restored correctly."""
        from toolrecall.adapters.google_adk import cached_tool

        call_count = [0]

        @cached_tool(ttl=60)
        def list_items(category: str) -> list:
            call_count[0] += 1
            return [f"{category}-item-{i}" for i in range(3)]

        r1 = list_items(category="tool")
        r2 = list_items(category="tool")

        assert r1 == r2
        assert call_count[0] == 1
        assert len(r1) == 3

    def test_none_return_type(self):
        """Tools returning None are cached correctly."""
        from toolrecall.adapters.google_adk import cached_tool

        call_count = [0]

        @cached_tool(ttl=60)
        def health_check() -> None:
            call_count[0] += 1
            return None

        r1 = health_check()
        r2 = health_check()

        assert r1 is None
        assert r2 is None
        assert call_count[0] == 1

    def test_int_return_type(self):
        """Tools returning ints are cached correctly."""
        from toolrecall.adapters.google_adk import cached_tool

        call_count = [0]

        @cached_tool(ttl=60)
        def count_items(tag: str) -> int:
            call_count[0] += 1
            return 42

        r1 = count_items(tag="any")
        r2 = count_items(tag="any")

        assert r1 == 42
        assert r2 == 42
        assert call_count[0] == 1

    def test_async_cached_tool(self):
        """Async tools work with @cached_tool."""
        from toolrecall.adapters.google_adk import cached_tool
        import asyncio

        call_count = [0]

        @cached_tool(ttl=60)
        async def fetch_data(url: str) -> str:
            call_count[0] += 1
            return f"data from {url}"

        r1 = asyncio.run(fetch_data(url="https://example.com"))
        r2 = asyncio.run(fetch_data(url="https://example.com"))

        assert r1 == r2
        assert call_count[0] == 1

    def test_nested_deserialization(self):
        """Deeply nested structures survive round-trip."""
        from toolrecall.adapters.google_adk import cached_tool

        call_count = [0]

        @cached_tool(ttl=60)
        def nested() -> dict:
            call_count[0] += 1
            return {
                "level1": {
                    "level2": {
                        "level3": ["a", "b", {"c": 42}]
                    }
                },
                "flag": True,
                "count": 0,
            }

        r1 = nested()
        r2 = nested()

        assert r1 == r2
        assert call_count[0] == 1
        assert r1["level1"]["level2"]["level3"][2]["c"] == 42
        assert r1["flag"] is True
        assert r1["count"] == 0


# ---- Full ADK Agent Integration ----

class TestADKAgentE2E:
    """Run a real ADK agent with @cached_tool through the LLM.

    These tests require a GEMINI_API_KEY or valid credentials.
    They are marked 'adk-llm' and skipped by default — run with:
      pytest tests/test_e2e_adk_adapter.py -m "adk-llm"
    """

    @pytest.mark.adk_llm
    def test_agent_runs_with_cached_tool(self):
        """Full ADK agent with cached tool."""
        from toolrecall.adapters.google_adk import cached_tool
        from google.adk import Agent, Runner
        from google.adk.sessions import InMemorySessionService
        from google.genai import types

        call_count = [0]

        @cached_tool(ttl=120)
        def get_weather(city: str) -> str:
            call_count[0] += 1
            return f"The weather in {city} is sunny, 22°C."

        agent = Agent(
            name="weather_agent",
            model="gemini-2.5-flash",
            instruction="You are a helpful weather assistant.",
            tools=[get_weather],
        )

        session_service = InMemorySessionService()
        runner = Runner(
            agent=agent,
            app_name="weather_app",
            session_service=session_service,
            auto_create_session=True,
        )

        # Run the agent
        events = list(runner.run(
            user_id="test_user",
            session_id="test-session-1",
            new_message=types.Content(
                role="user",
                parts=[types.Part(text="What's the weather in Berlin?")],
            ),
        ))

        model_replies = [
            ev.content.parts[0].text
            for ev in events
            if hasattr(ev, 'content') and ev.content
            and ev.content.role == "model"
            and ev.content.parts
            and hasattr(ev.content.parts[0], 'text')
        ]
        assert len(model_replies) > 0, "Should have at least one model reply"
        assert call_count[0] == 1, "Tool should have been called once"

        # Second call — should hit the cache
        events2 = list(runner.run(
            user_id="test_user",
            session_id="test-session-2",
            new_message=types.Content(
                role="user",
                parts=[types.Part(text="What's the weather in Berlin?")],
            ),
        ))

        assert call_count[0] == 1, "Tool should NOT be called — cached result returned"