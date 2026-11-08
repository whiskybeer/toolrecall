"""Tests for per-agent cache stats tracking.

Covers agent_cache_stats table upsert, backward compatibility,
cli reporting, and cache-key purity (agent_id never enters cache keys).
"""

import sys
import pytest
from toolrecall.cache import _record, get_agent_stats, _db, _init
from toolrecall.client import _resolve_agent_id, _add_agent_id


# ─── Fixtures ────────────────────────────────────────

@pytest.fixture(autouse=True)
def _ensure_schema():
    """Ensure agent_cache_stats table exists before each test."""
    _init()  # idempotent — creates tables if missing


@pytest.fixture
def _clean_agent_stats():
    """Clean agent_cache_stats before each test that uses it."""
    with _db() as conn:
        conn.execute("DELETE FROM agent_cache_stats")
    yield


# ─── Tests ───────────────────────────────────────────

class TestAgentTracking:

    def test_record_hit_with_agent_id(self, _clean_agent_stats):
        """Record a cache hit WITH agent_id → agent_cache_stats row appears."""
        _record(
            "file_cache",
            hit=True,
            tokens_saved=100,
            context_tokens=50,
            path="/tmp/test.txt",
            agent_id="test-agent",
        )
        stats = get_agent_stats(agent_id="test-agent")
        assert len(stats) == 1
        row = stats[0]
        assert row["agent_id"] == "test-agent"
        assert row["category"] == "file_cache"
        assert row["hits"] == 1
        assert row["misses"] == 0
        assert row["tokens_saved"] == 100
        assert row["context_tokens_saved"] == 50

    def test_record_without_agent_id_backward_compat(self, _clean_agent_stats, monkeypatch):
        """Record WITHOUT agent_id → no agent_cache_stats row (backward compat)."""
        monkeypatch.delenv("TOOLRECALL_AGENT_ID", raising=False)
        monkeypatch.delenv("TOOLRECALL_AGENT", raising=False)
        _record("file_cache", hit=True, tokens_saved=50, path="/tmp/test.txt")
        stats = get_agent_stats()
        assert stats == [], "Should be empty when no agent_id passed"

    def test_record_miss_with_agent_id(self, _clean_agent_stats):
        """Record a cache miss WITH agent_id → miss counter incremented."""
        _record(
            "file_cache",
            hit=False,
            tokens_read=200,
            path="/tmp/miss.txt",
            agent_id="test-agent",
        )
        stats = get_agent_stats(agent_id="test-agent")
        assert len(stats) == 1
        assert stats[0]["hits"] == 0
        assert stats[0]["misses"] == 1
        assert stats[0]["tokens_read_from_disk"] == 200

    def test_get_agent_stats_unfiltered(self, _clean_agent_stats):
        """get_agent_stats() with no filter returns ALL agents."""
        _record("file_cache", hit=True, tokens_saved=10, agent_id="agent-a")
        _record("terminal_cache", hit=True, tokens_saved=20, agent_id="agent-b")
        stats = get_agent_stats()
        assert len(stats) == 2
        agents = set(s["agent_id"] for s in stats)
        assert agents == {"agent-a", "agent-b"}

    def test_multiple_agents_independent(self, _clean_agent_stats):
        """Two agents' stats accumulate independently."""
        _record("file_cache", hit=True, tokens_saved=100, agent_id="alice")
        _record("file_cache", hit=False, tokens_read=50, agent_id="bob")
        _record("file_cache", hit=True, tokens_saved=200, agent_id="alice")

        alice_stats = get_agent_stats(agent_id="alice")
        assert len(alice_stats) == 1
        assert alice_stats[0]["hits"] == 2
        assert alice_stats[0]["misses"] == 0
        assert alice_stats[0]["tokens_saved"] == 300

        bob_stats = get_agent_stats(agent_id="bob")
        assert len(bob_stats) == 1
        assert bob_stats[0]["hits"] == 0
        assert bob_stats[0]["misses"] == 1
        assert bob_stats[0]["tokens_read_from_disk"] == 50

    def test_agent_id_not_in_cache_key(self, _clean_agent_stats):
        """agent_id is NOT part of cache keys — same path without agent_id
        still produces a cache-valid read."""
        with _db() as conn:
            cols = conn.execute("PRAGMA table_info(file_cache)").fetchall()
            col_names = [c["name"] for c in cols]
        assert "agent_id" not in col_names, (
            "agent_id must NOT be a column in file_cache — it's stats-only metadata"
        )

    def test_env_var_resolution(self, monkeypatch):
        """TOOLRECALL_AGENT_ID is resolved when explicit agent_id is None."""
        monkeypatch.setenv("TOOLRECALL_AGENT_ID", "env-agent")
        assert _resolve_agent_id() == "env-agent"
        assert _resolve_agent_id("explicit") == "explicit"

        payload = {"cmd": "test"}
        _add_agent_id(payload)
        assert payload["agent_id"] == "env-agent"

    def test_cli_agent_commands(self, _clean_agent_stats, monkeypatch, capsys):
        """CLI toolrecall agent ls/show/prune commands work correctly."""
        from toolrecall.cli import cmd_agent

        _record("file_cache", hit=True, tokens_saved=100, agent_id="agent-x")
        _record("terminal_cache", hit=False, tokens_read=50, agent_id="agent-x")

        # test ls
        monkeypatch.setattr(sys, "argv", ["toolrecall", "agent", "ls"])
        cmd_agent()
        captured = capsys.readouterr()
        assert "agent-x" in captured.out
        assert "hits=1" in captured.out

        # test show
        monkeypatch.setattr(sys, "argv", ["toolrecall", "agent", "show", "agent-x"])
        cmd_agent()
        captured = capsys.readouterr()
        assert "Agent: agent-x" in captured.out
        assert "file_cache" in captured.out
        assert "terminal_cache" in captured.out

        # test prune
        monkeypatch.setattr(sys, "argv", ["toolrecall", "agent", "prune", "agent-x"])
        cmd_agent()
        captured = capsys.readouterr()
        assert "Deleted stats for agent 'agent-x'" in captured.out

        # verify prune removed stats
        assert get_agent_stats(agent_id="agent-x") == []

    def test_cli_stats_by_agent_works(self, _clean_agent_stats, monkeypatch, capsys):
        """Verify toolrecall stats --by-agent outputs per-agent breakdown."""
        from toolrecall.cli import cmd_stats

        _record("file_cache", hit=True, tokens_saved=100, agent_id="cli-agent")
        monkeypatch.setattr(sys, "argv", ["toolrecall", "stats", "--by-agent"])
        cmd_stats()
        captured = capsys.readouterr()
        assert "Per-Agent Cache Stats" in captured.out
        assert "cli-agent" in captured.out
