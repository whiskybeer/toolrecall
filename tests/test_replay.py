"""Tests for toolrecall.replay — Recording and replay of tool call scenarios."""

import pytest
from toolrecall.replay import (
    ReplayManager,
    start_recording,
    start_replay,
    stop,
    status,
    intercept_call,
    record_response,
    _active,
)


class TestReplayManager:
    """Core ReplayManager — record, replay, list, delete, export, import."""

    @pytest.fixture(autouse=True)
    def _clean_vcr_state(self):
        """Reset Replay mode and clean up scenarios between tests."""
        stop()  # reset _active singleton
        _active.stop()  # ensure clean state
        self.replay = ReplayManager()
        # Clean all scenarios
        for s in self.replay.list_scenarios():
            self.replay.delete_scenario(s["name"])
        yield
        stop()
        for s in self.replay.list_scenarios():
            self.replay.delete_scenario(s["name"])

    def _make_db_path(self):
        """Force replay to use a test-specific SQLite DB via env var."""
        # ReplayManager uses _db() which is the global singleton DB.
        # Tests rely on the Hermes test fixture that sets HERMES_HOME to temp.
        # If the DB is pre-initialized, replay table creation happens automatically.
        return None  # Use default (toolrecall._db manages the connection)

    # ── Recording ───────────────────────────────────────────────────────

    def test_record_single_call(self):
        result = self.replay.record_call("test1", "cached_read",
                                       {"path": "/tmp/test.txt"},
                                       {"output": "file content", "cached": False})
        assert result["scenario"] == "test1"
        assert result["call_index"] == 0
        assert len(result["args_hash"]) == 16

    def test_record_multiple_calls_increments_index(self):
        self.replay.record_call("test1", "cached_read", {"path": "/tmp/a"}, "resp A")
        self.replay.record_call("test1", "cached_read", {"path": "/tmp/b"}, "resp B")
        scenarios = self.replay.list_scenarios()
        assert len(scenarios) == 1
        assert scenarios[0]["call_count"] == 2

    def test_record_strips_noise_keys(self):
        """Timestamps and session IDs should be stripped before hashing."""
        r1 = self.replay.record_call("test1", "cached_read",
                                   {"path": "/tmp/x", "timestamp": "2026-07-09"},
                                   "resp")
        r2 = self.replay.record_call("test1", "cached_read",
                                   {"path": "/tmp/x", "session_id": "sess-abc"},
                                   "resp")
        # Both should have the same args_hash (noise stripped)
        assert r1["args_hash"] == r2["args_hash"]

    def test_record_sorts_keys(self):
        """Key order shouldn't matter for the hash."""
        r1 = self.replay.record_call("test1", "cached_read",
                                   {"b": 2, "a": 1}, "resp")
        r2 = self.replay.record_call("test1", "cached_read",
                                   {"a": 1, "b": 2}, "resp")
        assert r1["args_hash"] == r2["args_hash"]

    # ── Replay ──────────────────────────────────────────────────────────

    def test_find_replay_hit(self):
        self.replay.record_call("test1", "cached_read",
                              {"path": "/tmp/test.txt"},
                              {"output": "file content", "cached": True})
        result = self.replay.find_replay("test1", "cached_read",
                                       {"path": "/tmp/test.txt"})
        assert result is not None
        assert result["output"] == "file content"

    def test_find_replay_miss_wrong_tool(self):
        self.replay.record_call("test1", "cached_read",
                              {"path": "/tmp/test.txt"}, "resp")
        result = self.replay.find_replay("test1", "cached_terminal",
                                       {"command": "hostname"})
        assert result is None

    def test_find_replay_miss_wrong_scenario(self):
        self.replay.record_call("test1", "cached_read",
                              {"path": "/tmp/test.txt"}, "resp")
        result = self.replay.find_replay("test2", "cached_read",
                                       {"path": "/tmp/test.txt"})
        assert result is None

    def test_find_replay_hit_noise_keys_stripped(self):
        """Replay should hit despite different noise keys."""
        self.replay.record_call("test1", "cached_read",
                              {"path": "/tmp/x", "timestamp": "t1"}, "cached resp")
        result = self.replay.find_replay("test1", "cached_read",
                                       {"path": "/tmp/x", "session_id": "s2"})
        assert result is not None
        assert result == "cached resp"

    def test_find_replay_hit_sorted_keys(self):
        """Replay should hit regardless of key order."""
        self.replay.record_call("test1", "cached_read",
                              {"b": 2, "a": 1}, "sorted resp")
        result = self.replay.find_replay("test1", "cached_read",
                                       {"a": 1, "b": 2})
        assert result is not None

    # ── Scenario Management ────────────────────────────────────────────

    def test_list_scenarios(self):
        self.replay.record_call("alpha", "cached_read", {"path": "/a"}, "r1")
        self.replay.record_call("beta", "cached_read", {"path": "/b"}, "r2")
        scenarios = self.replay.list_scenarios()
        names = [s["name"] for s in scenarios]
        assert "alpha" in names
        assert "beta" in names
        assert len(scenarios) == 2

    def test_get_scenario(self):
        self.replay.record_call("test1", "cached_read",
                              {"path": "/a"}, "resp a")
        self.replay.record_call("test1", "cached_terminal",
                              {"command": "hostname"}, "resp b")
        calls = self.replay.get_scenario("test1")
        assert len(calls) == 2
        assert calls[0]["tool_name"] == "cached_read"
        assert calls[1]["tool_name"] == "cached_terminal"

    def test_delete_scenario(self):
        self.replay.record_call("test1", "cached_read", {"path": "/a"}, "r")
        count = self.replay.delete_scenario("test1")
        assert count == 1
        assert len(self.replay.list_scenarios()) == 0

    def test_delete_nonexistent_scenario(self):
        count = self.replay.delete_scenario("nonexistent")
        assert count == 0

    # ── Export / Import ─────────────────────────────────────────────────

    def test_export_scenario(self):
        self.replay.record_call("test1", "cached_read",
                              {"path": "/a"}, {"output": "data"})
        exported = self.replay.export_scenario("test1")
        assert exported["toolrecall_replay_export"] is True
        assert exported["scenario_name"] == "test1"
        assert exported["call_count"] == 1
        assert exported["calls"][0]["tool_name"] == "cached_read"

    def test_export_empty_scenario(self):
        exported = self.replay.export_scenario("empty")
        assert exported["call_count"] == 0
        assert exported["calls"] == []

    def test_import_scenario(self):
        exported = {
            "toolrecall_replay_export": True,
            "version": 1,
            "scenario_name": "imported",
            "exported_at": 1000.0,
            "call_count": 2,
            "calls": [
                {"call_index": 0, "tool_name": "cached_read",
                 "args": {"path": "/a"}, "response": "resp a"},
                {"call_index": 1, "tool_name": "cached_terminal",
                 "args": {"command": "hostname"}, "response": "resp b"},
            ],
        }
        result = self.replay.import_scenario(exported)
        assert result["scenario_name"] == "imported"
        assert result["calls_imported"] == 2
        assert len(self.replay.get_scenario("imported")) == 2

    def test_import_rejects_invalid_format(self):
        with pytest.raises(ValueError, match="Not a valid Replay export"):
            self.replay.import_scenario({"some": "data"})

    def test_import_overwrite_flag(self):
        self.replay.record_call("dup", "cached_read", {"path": "/a"}, "original")
        # Import with overwrite=False (default) — existing entries preserved
        exported = {
            "toolrecall_replay_export": True,
            "version": 1,
            "scenario_name": "dup",
            "exported_at": 2000.0,
            "call_count": 1,
            "calls": [{"call_index": 0, "tool_name": "cached_read",
                       "args": {"path": "/a"}, "response": "new"}],
        }
        result = self.replay.import_scenario(exported, overwrite=False)
        # Without overwrite, existing entries are preserved (INSERT OR IGNORE)
        # but the import still counts attempted calls
        assert result["calls_imported"] == 1
        # Original data should still be there
        orig = self.replay.find_replay("dup", "cached_read", {"path": "/a"})
        assert orig == "original"
        # Now with overwrite=True
        result = self.replay.import_scenario(exported, overwrite=True)
        assert result["calls_imported"] == 1

    # ── Round-trip: export → import ─────────────────────────────────────

    def test_export_import_roundtrip(self):
        self.replay.record_call("orig", "cached_read",
                              {"path": "/data"}, {"result": "ok"})
        exported = self.replay.export_scenario("orig")
        # Import into a new name
        exported["scenario_name"] = "copy"
        result = self.replay.import_scenario(exported)
        assert result["calls_imported"] == 1
        # Verify replay works on imported data
        replayed = self.replay.find_replay("copy", "cached_read", {"path": "/data"})
        assert replayed == {"result": "ok"}


class TestReplayMode:
    """Replay mode singleton — start_recording, start_replay, stop, status."""

    @pytest.fixture(autouse=True)
    def _clean(self):
        stop()
        _active.stop()
        yield
        stop()
        _active.stop()

    def test_start_recording(self):
        result = start_recording("my-test")
        assert result["replay_mode"] == "record"
        assert result["scenario"] == "my-test"
        assert _active.is_recording()
        assert not _active.is_replaying()

    def test_start_replay(self):
        result = start_replay("my-test")
        assert result["replay_mode"] == "replay"
        assert result["scenario"] == "my-test"
        assert _active.is_replaying()
        assert not _active.is_recording()

    def test_stop(self):
        start_recording("test")
        result = stop()
        assert result["replay_mode"] == "stopped"
        assert result["was"] == "record"
        assert not _active.is_active()

    def test_stop_inactive(self):
        result = stop()
        assert result["replay_mode"] == "inactive"
        assert not _active.is_active()

    def test_switch_mode(self):
        """Starting a new scenario should override the previous one."""
        start_recording("first")
        start_replay("second")
        assert _active.is_replaying()
        assert _active.scenario == "second"

    def test_status_inactive(self):
        s = status()
        assert s["replay_mode"] == "inactive"
        assert not s["is_active"]

    def test_status_recording(self):
        start_recording("test-rec")
        s = status()
        assert s["replay_mode"] == "record"
        assert s["scenario"] == "test-rec"
        assert s["is_recording"]
        assert not s["is_replaying"]

    def test_status_replaying(self):
        start_replay("test-rep")
        s = status()
        assert s["replay_mode"] == "replay"
        assert s["scenario"] == "test-rep"
        assert s["is_replaying"]
        assert not s["is_recording"]


class TestReplayIntercept:
    """Replay intercept_call and record_response — daemon integration points."""

    @pytest.fixture(autouse=True)
    def _clean(self):
        replay = ReplayManager()
        for s in replay.list_scenarios():
            replay.delete_scenario(s["name"])
        stop()
        _active.stop()
        yield
        stop()
        _active.stop()
        replay = ReplayManager()
        for s in replay.list_scenarios():
            replay.delete_scenario(s["name"])

    def test_intercept_inactive_returns_none(self):
        result = intercept_call("cached_read", {"path": "/tmp/x"})
        assert result is None

    def test_intercept_recording_returns_no_hit_marker(self):
        start_recording("test-rec")
        result = intercept_call("cached_read", {"path": "/tmp/x"})
        assert result is not None
        assert not result["replay_hit"]
        assert result["message"] == "recording"

    def test_intercept_replay_hit(self):
        replay = ReplayManager()
        replay.record_call("test-rep", "cached_read",
                         {"path": "/tmp/x"}, {"output": "cached"})
        start_replay("test-rep")
        result = intercept_call("cached_read", {"path": "/tmp/x"})
        assert result is not None
        assert result["replay_hit"]
        assert result["data"] == {"output": "cached"}

    def test_intercept_replay_miss(self):
        start_replay("test-rep")
        result = intercept_call("cached_read", {"path": "/tmp/x"})
        assert result is not None
        assert not result["replay_hit"]
        assert "No replay found" in result["message"]

    def test_intercept_replay_hit_despite_noise(self):
        replay = ReplayManager()
        replay.record_call("test-rep", "cached_read",
                         {"path": "/tmp/x", "timestamp": "t1"}, "data")
        start_replay("test-rep")
        result = intercept_call("cached_read",
                                 {"path": "/tmp/x", "session_id": "s2"})
        assert result is not None
        assert result["replay_hit"]

    def test_record_response(self):
        start_recording("test-rec")
        result = record_response("cached_read", {"path": "/tmp/x"},
                                  {"output": "file content"})
        assert result["scenario"] == "test-rec"
        # Verify it was actually recorded
        replay = ReplayManager()
        replayed = replay.find_replay("test-rec", "cached_read", {"path": "/tmp/x"})
        assert replayed == {"output": "file content"}

    def test_record_response_when_not_recording(self):
        result = record_response("cached_read", {"path": "/tmp/x"}, "data")
        assert result["replay_mode"] == "not_recording"

    def test_record_response_explicit_scenario(self):
        start_recording("default-scenario")
        record_response("cached_read", {"path": "/tmp/x"},
                                  "data", scenario="override-scenario")
        replay = ReplayManager()
        # Should be recorded under override, not default
        assert replay.find_replay("override-scenario", "cached_read",
                                {"path": "/tmp/x"}) == "data"
        assert replay.find_replay("default-scenario", "cached_read",
                                {"path": "/tmp/x"}) is None

    def test_full_record_replay_cycle(self):
        """Record a session, then replay it — end-to-end."""
        ReplayManager()
        # Phase 1: Record
        start_recording("full-cycle")
        resp1 = {"output": "file content", "cached": False}
        resp2 = {"output": "hostname output", "cached": False}
        record_response("cached_read", {"path": "/tmp/a"}, resp1)
        record_response("cached_terminal", {"command": "hostname"}, resp2)
        stop()

        # Phase 2: Replay
        start_replay("full-cycle")
        hit1 = intercept_call("cached_read", {"path": "/tmp/a"})
        assert hit1["replay_hit"]
        assert hit1["data"] == resp1

        hit2 = intercept_call("cached_terminal", {"command": "hostname"})
        assert hit2["replay_hit"]
        assert hit2["data"] == resp2

        # Miss — not recorded
        miss = intercept_call("cached_terminal", {"command": "whoami"})
        assert not miss["replay_hit"]