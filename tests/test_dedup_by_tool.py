"""Per-tool attribution of request-dedup savings (dedup.by_tool + dedup_stats).

Why this exists: the proxy logged one dedup_tokens number per request, so the
question "is skills_list being stubbed?" had no answer. Measured 2026-10-01 over
385 sessions: skills_list repeats byte-identically in 43 % of its large outputs
(19 of 44) — the highest rate of any tool — and those are exactly the blocks the
existing dedup already stubs. What was missing was being able to SEE it.
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from toolrecall.dedup import dedup_messages  # noqa: E402

SKILLS = "skill-a: does a thing\n" * 700  # ~15 KB, like a real skills_list result


def _call(call_id: str, name: str) -> dict:
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {"id": call_id, "type": "function", "function": {"name": name, "arguments": "{}"}}
        ],
    }


def _result(call_id: str, content: str) -> dict:
    return {"role": "tool", "tool_call_id": call_id, "content": content}


def _two_skills_lists_then_tail() -> list:
    return [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "list skills"},
        _call("c1", "skills_list"),
        _result("c1", SKILLS),
        _call("c2", "skills_list"),
        _result("c2", SKILLS),
        {"role": "user", "content": "and now?"},
        {"role": "assistant", "content": "ok"},
    ]


class TestAttribution:
    def test_repeated_skills_list_is_stubbed_and_attributed(self):
        out, stats = dedup_messages(_two_skills_lists_then_tail())
        assert stats["blocks"] == 1
        assert out[5]["content"].startswith("[toolrecall-dedup]"), "second copy is the stub"
        assert out[3]["content"] == SKILLS, "first copy is kept (prefix stability)"
        assert list(stats["by_tool"]) == ["skills_list"]
        assert stats["by_tool"]["skills_list"]["blocks"] == 1
        assert stats["by_tool"]["skills_list"]["chars_saved"] == stats["chars_saved"]

    def test_per_tool_sums_equal_the_request_totals(self):
        big_file = "x = 1\n" * 400
        msgs = [
            {"role": "user", "content": "go"},
            _call("a", "read_file"),
            _result("a", big_file),
            _call("b", "skills_list"),
            _result("b", SKILLS),
            _call("c", "read_file"),
            _result("c", big_file),
            _call("d", "skills_list"),
            _result("d", SKILLS),
            {"role": "user", "content": "tail 1"},
            {"role": "assistant", "content": "tail 2"},
        ]
        _, stats = dedup_messages(msgs)
        assert set(stats["by_tool"]) == {"read_file", "skills_list"}
        assert sum(s["blocks"] for s in stats["by_tool"].values()) == stats["blocks"]
        assert sum(s["chars_saved"] for s in stats["by_tool"].values()) == stats["chars_saved"]

    def test_name_field_is_the_fallback_without_a_tool_call(self):
        msgs = [
            {"role": "user", "content": "go"},
            {"role": "tool", "name": "skills_list", "content": SKILLS},
            {"role": "tool", "name": "skills_list", "content": SKILLS},
            {"role": "user", "content": "tail 1"},
            {"role": "assistant", "content": "tail 2"},
        ]
        _, stats = dedup_messages(msgs)
        assert list(stats["by_tool"]) == ["skills_list"]

    def test_unknown_tool_is_labelled_not_dropped(self):
        msgs = [
            {"role": "user", "content": "go"},
            {"role": "tool", "content": SKILLS},
            {"role": "tool", "content": SKILLS},
            {"role": "user", "content": "tail 1"},
            {"role": "assistant", "content": "tail 2"},
        ]
        _, stats = dedup_messages(msgs)
        assert list(stats["by_tool"]) == ["unknown-tool"]

    def test_protected_tail_is_still_not_stubbed(self):
        """Attribution must not change WHAT is stubbed — only label it."""
        msgs = _two_skills_lists_then_tail()[:6]  # duplicate sits in the last 2
        out, stats = dedup_messages(msgs)
        assert stats["blocks"] == 0
        assert stats["by_tool"] == {}
        assert out[5]["content"] == SKILLS

    def test_no_duplicates_means_empty_attribution(self):
        _, stats = dedup_messages([{"role": "user", "content": "hi"}])
        assert stats["by_tool"] == {}


@pytest.fixture()
def _fresh_db(_isolated_toolrecall_env):
    """Point the cache-DB singleton at this test's temp DB (see conftest)."""
    import toolrecall._db as dbmod

    def _reset():
        if getattr(dbmod, "_db_real", None) is not None:
            try:
                dbmod._db_real.close()
            except Exception:
                pass
            dbmod._db_real = None
        dbmod._cached_config = None

    _reset()
    from toolrecall.cache import SCHEMA

    dbmod._init(SCHEMA)
    yield
    _reset()


class TestLedger:
    def test_record_accumulates_per_tool(self, _fresh_db):
        from toolrecall import dedup_stats

        dedup_stats.record({"skills_list": {"blocks": 1, "chars_saved": 4000}})
        dedup_stats.record(
            {
                "skills_list": {"blocks": 2, "chars_saved": 8000},
                "read_file": {"blocks": 1, "chars_saved": 400},
            }
        )
        rows = {r["tool"]: r for r in dedup_stats.per_tool()}
        assert rows["skills_list"]["blocks"] == 3
        assert rows["skills_list"]["chars_saved"] == 12000
        assert rows["skills_list"]["tokens_saved"] == 3000, "same chars//4 estimator as the CSV"
        assert rows["skills_list"]["requests"] == 2
        assert rows["read_file"]["requests"] == 1
        assert [r["tool"] for r in dedup_stats.per_tool()][0] == "skills_list", "largest first"

    def test_empty_input_writes_nothing(self, _fresh_db):
        from toolrecall import dedup_stats

        dedup_stats.record({})
        assert dedup_stats.per_tool() == []

    def test_empty_ledger_says_it_is_not_proof_of_breakage(self, _fresh_db):
        import io
        from contextlib import redirect_stdout

        import toolrecall.cli as cli

        buf = io.StringIO()
        with redirect_stdout(buf):
            cli.cmd_dedup_stats()
        assert "NOT proof" in buf.getvalue()


class TestProxyPath:
    def test_proxy_dedup_body_carries_the_attribution(self, monkeypatch):
        import toolrecall.proxy as proxy_mod

        monkeypatch.setenv("TOOLRECALL_FORWARD_DEDUP", "true")
        body = json.dumps({"model": "m", "messages": _two_skills_lists_then_tail()}).encode()
        out, stats = proxy_mod._maybe_dedup_body(body)
        assert stats and stats["blocks"] == 1
        assert "skills_list" in stats["by_tool"]
        assert b"[toolrecall-dedup]" in out
