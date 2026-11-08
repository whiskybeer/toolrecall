"""Tests for the agent-facing tool usage counter and the docs tool descriptions.

Context (2026-09-30): ``docs_search`` and ``docs_get_page`` had been advertised
in the bridge for months and had **never been called once** — verified against
the Hermes agent log (per-tool completion lines) and the session transcripts.
Nothing in ToolRecall could have shown that, and the model-facing description
even carried a leaked developer note ("Hide from tools/list when knowledge DB is
empty"), i.e. an instruction to hide the tool, plus no hint of *when* to use it.

So these tests pin two different things:
  1. the counter records calls and failures (so the next answer is measurable),
  2. the descriptions stay model-facing text with a usage trigger and no notes.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture(autouse=True)
def _reset_db_singleton(_isolated_toolrecall_env):
    """Make conftest's per-test TOOLRECALL_CACHE_DB take effect.

    toolrecall._db caches config + connection as module singletons; without this
    reset the counter writes into the FIRST test's temp DB.
    """
    import toolrecall._db as dbmod

    dbmod._cached_config = None
    if getattr(dbmod, "_db_real", None) is not None:
        try:
            dbmod._db_real.close()
        except Exception:
            pass
        dbmod._db_real = None
    from toolrecall.cache import SCHEMA

    dbmod._init(SCHEMA)
    yield
    if getattr(dbmod, "_db_real", None) is not None:
        try:
            dbmod._db_real.close()
        except Exception:
            pass
        dbmod._db_real = None
        dbmod._cached_config = None


def _counts() -> dict:
    from toolrecall.cache import tool_usage_counts

    return {r["tool"]: r for r in tool_usage_counts()}


class TestCounter:
    def test_calls_and_errors_accumulate(self):
        from toolrecall.cache import record_tool_call

        record_tool_call("docs_search")
        record_tool_call("docs_search", ok=False)
        rows = _counts()
        assert rows["docs_search"]["calls"] == 2
        assert rows["docs_search"]["errors"] == 1, "a failing call must not look like no call"
        assert rows["docs_search"]["first_at"] is not None

    def test_untracked_tools_are_ignored(self):
        """File/shim volume is a different question — mixing it in buries the point."""
        from toolrecall.cache import record_tool_call

        record_tool_call("read_file")
        record_tool_call("terminal")
        assert _counts() == {}

    def test_empty_counter_explains_itself(self):
        """An empty table must not read as 'agents do not use ToolRecall'."""
        import io
        from contextlib import redirect_stdout

        import toolrecall.cli as cli

        buf = io.StringIO()
        with redirect_stdout(buf):
            cli.cmd_tool_usage()
        out = buf.getvalue()
        assert "NOT proof" in out
        assert "not counted" in out


class TestDaemonWrapping:
    def test_success_counts_as_a_call_without_error(self):
        from toolrecall.daemon import _count_tool_call

        def handler(request):
            return {"result": "fine"}

        assert _count_tool_call("mcp_call", handler, {}) == {"result": "fine"}
        assert _counts()["mcp_call"]["calls"] == 1
        assert _counts()["mcp_call"]["errors"] == 0

    def test_error_payload_counts_as_an_error(self):
        from toolrecall.daemon import _count_tool_call

        def handler(request):
            return {"error": "server not allowed"}

        _count_tool_call("docs_get_page", handler, {})
        assert _counts()["docs_get_page"]["errors"] == 1

    def test_exception_is_counted_and_still_raised(self):
        """The counter must never turn a working tool call into a failed one —
        and it must never swallow the failure it counted."""
        from toolrecall.daemon import _count_tool_call

        def handler(request):
            raise RuntimeError("boom")

        with pytest.raises(RuntimeError):
            _count_tool_call("docs_search", handler, {})
        assert _counts()["docs_search"]["errors"] == 1


class TestModelFacingDescriptions:
    """The description is the only text the model sees for a tool."""

    def _descriptions(self) -> dict:
        import toolrecall.mcp_bridge as bridge

        return {t["name"]: t["description"] for t in bridge.TOOL_DEFINITIONS}

    def test_no_developer_note_leaks_into_the_description(self):
        desc = self._descriptions()["docs_search"]
        assert "tools/list" not in desc, "a 'hide from tools/list' note must not reach the model"
        assert "Hide from" not in desc and "FTS5" not in desc

    def test_docs_search_description_states_when_to_use_it(self):
        desc = self._descriptions()["docs_search"]
        assert "knowledge base" in desc
        assert "past decisions" in desc, "a trigger is what changes tool selection"
        assert "grep" in desc, "name the alternative it must beat"

    def test_docs_get_page_points_back_at_docs_search(self):
        assert "docs_search" in self._descriptions()["docs_get_page"]
