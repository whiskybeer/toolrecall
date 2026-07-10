"""Replay Mode — Record and Replay agent tool calls for deterministic testing.

Replay mode records live tool outputs to SQLite on
the first run, then replays them on subsequent runs — enabling deterministic,
offline, zero-cost CI testing.

Use cases:
- CI/CD: deterministic agent tests (no network, no costs, no flakiness)
- Debugging: replay exact conditions from a bug report
- Demos: guaranteed output without live API keys
- Offline development: work on an agent without network access

Scenarios are stored in SQLite alongside the regular cache, keyed by
scenario name + tool call hash. Each scenario is a collection of
(tool_name, args_hash, response) tuples.

Architecture:
    ReplayManager is the core — it manages scenarios, records calls, and
    replays them. The daemon checks Replay mode before calling the real cache.
    When recording, all tool outputs are stored. When replaying, matching
    calls return cached responses without executing anything.

Usage:
    >>> from toolrecall.replay import ReplayManager, start_recording, start_replay
    >>> replay = ReplayManager()
    >>> replay.record_call("test1", "cached_read", {"path": "/tmp/x"}, "file content")
    >>> start_replay("test1")
    >>> result = replay.find_replay("test1", "cached_read", {"path": "/tmp/x"})
    'file content'
    >>> stop()
"""

import json
import hashlib
import time
from typing import Any, Optional

from toolrecall._db import _db
from toolrecall.normalizer import normalize_tool_args, normalize_json


# ─── Constants ─────────────────────────────────────────────────────────────────

REPLAY_TABLE = "replay_scenarios"


# ─── Scenario Mode ─────────────────────────────────────────────────────────────

class _Mode:
    """Replay mode state — which scenario is active and whether we're recording or replaying."""
    __slots__ = ("scenario", "mode")

    def __init__(self):
        self.scenario: Optional[str] = None
        self.mode: Optional[str] = None  # "record" or "replay"

    def is_active(self) -> bool:
        return self.scenario is not None and self.mode is not None

    def is_recording(self) -> bool:
        return self.is_active() and self.mode == "record"

    def is_replaying(self) -> bool:
        return self.is_active() and self.mode == "replay"

    def start(self, scenario: str, mode: str):
        self.scenario = scenario
        self.mode = mode

    def stop(self):
        self.scenario = None
        self.mode = None


# Module-level singleton — daemon and CLI share this
_active = _Mode()


def active_mode() -> _Mode:
    """Return the module-level Replay mode singleton."""
    return _active


# ─── Helpers ───────────────────────────────────────────────────────────────────

def _make_hash(tool_name: str, args: dict) -> str:
    """Deterministic hash of tool name + normalized arguments."""
    args_json = normalize_tool_args(args)
    raw = f"{tool_name}:{args_json}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


# ─── ReplayManager ────────────────────────────────────────────────────────────────

class ReplayManager:
    """Manages recording and replay of tool call scenarios.

    Each scenario is a named collection of tool calls stored in a SQLite table.
    Scenarios are independent — you can record one interaction pattern at a time.

    The manager is stateless (all state is in SQLite or the module-level _active
    singleton). You can create multiple instances safely.
    """

    def __init__(self):
        self._ensure_table()

    def _ensure_table(self):
        """Create the Replay scenarios table if it doesn't exist."""
        with _db() as conn:
            conn.execute(f"""
                CREATE TABLE IF NOT EXISTS {REPLAY_TABLE} (
                    scenario_name TEXT NOT NULL,
                    call_index INTEGER NOT NULL,
                    tool_name TEXT NOT NULL,
                    args_hash TEXT NOT NULL,
                    args_json TEXT NOT NULL,
                    response_json TEXT NOT NULL,
                    recorded_at REAL NOT NULL,
                    PRIMARY KEY (scenario_name, call_index)
                )
            """)
            conn.execute(f"""
                CREATE INDEX IF NOT EXISTS idx_replay_lookup
                ON {REPLAY_TABLE} (scenario_name, tool_name, args_hash)
            """)

    # ── Recording ───────────────────────────────────────────────────────────

    def record_call(self, scenario: str, tool_name: str, args: dict, response: Any) -> dict:
        """Record a tool call and its response to a scenario.

        Args:
            scenario: Scenario name (e.g. "auth-flow", "debug-session-1")
            tool_name: Tool name (e.g. "cached_read", "cached_terminal")
            args: Tool arguments dict
            response: The response to cache (any JSON-serializable value)

        Returns:
            dict with scenario, call_index, and args_hash
        """
        args_json = normalize_tool_args(args)
        args_hash = _make_hash(tool_name, args)
        with _db() as conn:
            call_index = self._next_index(conn, scenario)
            conn.execute(
                f"INSERT INTO {REPLAY_TABLE} "
                f"(scenario_name, call_index, tool_name, args_hash, args_json, response_json, recorded_at) "
                f"VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    scenario,
                    call_index,
                    tool_name,
                    args_hash,
                    args_json,
                    json.dumps(response, default=str),
                    time.time(),
                ),
            )
        return {
            "scenario": scenario,
            "call_index": call_index,
            "args_hash": args_hash,
        }

    # ── Replay ──────────────────────────────────────────────────────────────

    def find_replay(self, scenario: str, tool_name: str, args: dict) -> Optional[Any]:
        """Find a matching recorded response for replay.

        Searches by (scenario, tool_name, args_hash). Returns the most
        recently recorded match — not necessarily the first one.

        Args:
            scenario: Scenario name to search in
            tool_name: Tool name to match
            args: Tool arguments (normalized before hashing)

        Returns:
            The cached response (deserialized from JSON), or None if no match
        """
        args_hash = _make_hash(tool_name, args)
        with _db() as conn:
            row = conn.execute(
                f"SELECT response_json FROM {REPLAY_TABLE} "
                f"WHERE scenario_name = ? AND tool_name = ? AND args_hash = ? "
                f"ORDER BY call_index DESC LIMIT 1",
                (scenario, tool_name, args_hash),
            ).fetchone()
            if row:
                return json.loads(row[0])
        return None

    # ── Scenario Management ────────────────────────────────────────────────

    def list_scenarios(self) -> list[dict]:
        """List all scenarios with metadata.

        Returns:
            List of dicts: {name, call_count, first_recorded, last_recorded}
        """
        with _db() as conn:
            rows = conn.execute(
                f"SELECT scenario_name, COUNT(*) as cnt, "
                f"MIN(recorded_at) as first_ts, MAX(recorded_at) as last_ts "
                f"FROM {REPLAY_TABLE} GROUP BY scenario_name ORDER BY scenario_name"
            ).fetchall()
        return [
            {
                "name": r["scenario_name"],
                "call_count": r["cnt"],
                "first_recorded": r["first_ts"],
                "last_recorded": r["last_ts"],
            }
            for r in rows
        ]

    def get_scenario(self, name: str) -> list[dict]:
        """Get all recorded calls in a scenario, ordered by call_index.

        Returns:
            List of dicts: {call_index, tool_name, args_json, response_json, recorded_at}
        """
        with _db() as conn:
            rows = conn.execute(
                f"SELECT call_index, tool_name, args_json, response_json, recorded_at "
                f"FROM {REPLAY_TABLE} WHERE scenario_name = ? ORDER BY call_index",
                (name,),
            ).fetchall()
        return [
            {
                "call_index": r["call_index"],
                "tool_name": r["tool_name"],
                "args": json.loads(r["args_json"]),
                "response": json.loads(r["response_json"]),
                "recorded_at": r["recorded_at"],
            }
            for r in rows
        ]

    def delete_scenario(self, name: str) -> int:
        """Delete a scenario and all its recorded calls.

        Returns:
            Number of deleted rows
        """
        with _db() as conn:
            cur = conn.execute(
                f"DELETE FROM {REPLAY_TABLE} WHERE scenario_name = ?", (name,)
            )
        return cur.rowcount

    def export_scenario(self, name: str) -> dict:
        """Export a scenario as a portable JSON dict.

        The exported JSON can be committed to git, shared, or imported
        on another machine. Contains all recorded calls with metadata.

        Returns:
            dict with scenario_name, exported_at, version, and calls array
        """
        calls = self.get_scenario(name)
        return {
            "toolrecall_replay_export": True,
            "version": 1,
            "scenario_name": name,
            "exported_at": time.time(),
            "call_count": len(calls),
            "calls": [
                {
                    "call_index": c["call_index"],
                    "tool_name": c["tool_name"],
                    "args": c["args"],
                    "response": c["response"],
                }
                for c in calls
            ],
        }

    def import_scenario(self, data: dict, overwrite: bool = False) -> dict:
        """Import a scenario from an exported JSON dict.

        Args:
            data: The exported scenario dict (from export_scenario)
            overwrite: If True, delete existing scenario with same name first

        Returns:
            dict with scenario_name, calls_imported
        """
        if not data.get("toolrecall_replay_export"):
            raise ValueError("Not a valid Replay export (missing toolrecall_replay_export marker)")

        name = data["scenario_name"]
        if overwrite:
            self.delete_scenario(name)

        with _db() as conn:
            imported = 0
            for call in data["calls"]:
                args_json = normalize_tool_args(call["args"])
                args_hash = _make_hash(call["tool_name"], call["args"])
                conn.execute(
                    f"INSERT OR IGNORE INTO {REPLAY_TABLE} "
                    f"(scenario_name, call_index, tool_name, args_hash, args_json, response_json, recorded_at) "
                    f"VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        name,
                        call["call_index"],
                        call["tool_name"],
                        args_hash,
                        args_json,
                        json.dumps(call["response"], default=str),
                        time.time(),
                    ),
                )
                if conn.total_changes:
                    imported += 1
        return {"scenario_name": name, "calls_imported": imported}

    # ── Internal ────────────────────────────────────────────────────────────

    def _next_index(self, conn, scenario: str) -> int:
        row = conn.execute(
            f"SELECT COALESCE(MAX(call_index), -1) + 1 as next_idx "
            f"FROM {REPLAY_TABLE} WHERE scenario_name = ?",
            (scenario,),
        ).fetchone()
        return row["next_idx"] if row else 0


# ─── Convenience functions ─────────────────────────────────────────────────────

def start_recording(scenario: str) -> dict:
    """Start recording mode for a scenario.

    Every tool call that goes through the daemon will be recorded.

    Returns:
        dict with status and scenario name
    """
    _active.start(scenario, "record")
    return {"replay_mode": "record", "scenario": scenario}


def start_replay(scenario: str) -> dict:
    """Start replay mode for a scenario.

    Tool calls matching recorded responses will be served from cache
    without executing anything.

    Returns:
        dict with status and scenario name
    """
    _active.start(scenario, "replay")
    return {"replay_mode": "replay", "scenario": scenario}


def stop() -> dict:
    """Stop Replay mode (both recording and replaying).

    Returns:
        dict with previous mode and scenario, or message if inactive
    """
    prev = {"mode": _active.mode, "scenario": _active.scenario}
    _active.stop()
    if prev["mode"] is None:
        return {"replay_mode": "inactive", "message": "Replay mode was not active"}
    return {"replay_mode": "stopped", "was": prev["mode"], "scenario": prev["scenario"]}


def status() -> dict:
    """Get current Replay mode status.

    Returns:
        dict with mode, scenario, and is_active
    """
    return {
        "replay_mode": _active.mode or "inactive",
        "scenario": _active.scenario,
        "is_active": _active.is_active(),
        "is_recording": _active.is_recording(),
        "is_replaying": _active.is_replaying(),
    }


def intercept_call(tool_name: str, args: dict) -> Optional[dict]:
    """Intercept a tool call for Recording/replay.

    Called by the daemon before executing a tool. If replay is active:
    - Replay mode: returns cached response if found, or None (miss)
    - Record mode: returns None (let it execute), the response is recorded
      by the caller via record_response()

    This is the integration point between the daemon and Replay mode.

    Args:
        tool_name: The tool being called
        args: The tool arguments

    Returns:
        Dict with {"replay_hit": True, "data": ...} on replay hit,
        or None if no replay action needed (normal execution)
    """
    if not _active.is_active():
        return None

    manager = ReplayManager()

    if _active.is_replaying():
        response = manager.find_replay(_active.scenario, tool_name, args)
        if response is not None:
            return {"replay_hit": True, "data": response}
        return {"replay_hit": False, "message": f"No replay found for {tool_name}"}

    if _active.is_recording():
        return {"replay_hit": False, "message": "recording"}

    return None


def record_response(tool_name: str, args: dict, response: Any, scenario: Optional[str] = None) -> dict:
    """Record a tool response after real execution.

    Called by the daemon after a tool executes successfully while replay
    recording is active.

    Args:
        tool_name: The tool that was called
        args: The tool arguments
        response: The response to record
        scenario: Override scenario name (uses active scenario by default)

    Returns:
        dict with recording result
    """
    if not _active.is_recording():
        return {"replay_mode": "not_recording"}

    actual_scenario = scenario or _active.scenario
    if actual_scenario is None:
        return {"replay_mode": "error", "message": "No active scenario"}

    manager = ReplayManager()
    return manager.record_call(actual_scenario, tool_name, args, response)