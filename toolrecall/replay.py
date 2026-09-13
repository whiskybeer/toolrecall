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
    replays them. The daemon does NOT yet check Replay mode — this is a
    planned integration (see replay_cli.py for status). When wired,
    recording stores all tool outputs and replay returns cached responses
    without executing anything.

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
import time
import uuid
from typing import Any, Optional

from toolrecall._db import _db
from toolrecall.normalizer import normalize_tool_args
from toolrecall.keys import content_digest


# ─── Constants ─────────────────────────────────────────────────────────────────

REPLAY_TABLE = "replay_scenarios"
REPLAY_TREE_NODES = "replay_tree_nodes"
REPLAY_TREE_CALLS = "replay_tree_calls"


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
# NOTE: This is in-process only. Daemon and CLI are separate processes,
# so the mode state does not cross process boundaries. This is a known
# limitation — see replay_cli.py for status.
_active = _Mode()


def active_mode() -> _Mode:
    """Return the module-level Replay mode singleton."""
    return _active


# ─── Helpers ───────────────────────────────────────────────────────────────────


def _make_hash(tool_name: str, args: dict) -> str:
    """Deterministic hash of tool name + normalized arguments."""
    args_json = normalize_tool_args(args)
    raw = f"{tool_name}:{args_json}"
    return content_digest(raw, truncate=16)


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
            cur = conn.execute(f"DELETE FROM {REPLAY_TABLE} WHERE scenario_name = ?", (name,))
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

    # ── Tree / fork primitives (G1) ────────────────────────────────────────

    def _ensure_tree_tables(self):
        """Create the fork-tree tables if they don't exist.

        Separate from _ensure_table() so legacy scenarios pay nothing and
        the flat table's schema never changes (no migration).
        """
        with _db() as conn:
            conn.execute(f"""
                CREATE TABLE IF NOT EXISTS {REPLAY_TREE_NODES} (
                    tree_id TEXT NOT NULL,
                    node_id TEXT NOT NULL,
                    parent_id TEXT,
                    branch_name TEXT NOT NULL,
                    depth INTEGER NOT NULL,
                    created_at REAL NOT NULL,
                    PRIMARY KEY (tree_id, node_id)
                )
            """)
            conn.execute(f"""
                CREATE TABLE IF NOT EXISTS {REPLAY_TREE_CALLS} (
                    tree_id TEXT NOT NULL,
                    node_id TEXT NOT NULL,
                    call_index INTEGER NOT NULL,
                    tool_name TEXT NOT NULL,
                    args_hash TEXT NOT NULL,
                    args_json TEXT NOT NULL,
                    response_json TEXT NOT NULL,
                    recorded_at REAL NOT NULL,
                    PRIMARY KEY (tree_id, node_id, call_index)
                )
            """)
            conn.execute(f"""
                CREATE INDEX IF NOT EXISTS idx_replay_tree_lookup
                ON {REPLAY_TREE_CALLS} (tree_id, node_id, tool_name, args_hash)
            """)
            conn.execute(f"""
                CREATE INDEX IF NOT EXISTS idx_replay_tree_nodes
                ON {REPLAY_TREE_NODES} (tree_id, parent_id)
            """)

    def fork_tree(self, tree_name: str) -> dict:
        """Create a new replay tree with a root node.

        Args:
            tree_name: Human-readable tree label (e.g. a scenario name)

        Returns:
            dict with tree_id, root (node_id), name
        """
        self._ensure_tree_tables()
        tree_id = uuid.uuid4().hex[:16]
        root_id = uuid.uuid4().hex[:16]
        with _db() as conn:
            conn.execute(
                f"INSERT INTO {REPLAY_TREE_NODES} "
                f"(tree_id, node_id, parent_id, branch_name, depth, created_at) "
                f"VALUES (?, ?, NULL, ?, 0, ?)",
                (tree_id, root_id, tree_name, time.time()),
            )
        return {"tree_id": tree_id, "root": root_id, "name": tree_name}

    def fork_node(self, tree_id: str, parent_node_id: str, branch_name: str) -> dict:
        """Fork a new branch node under a parent node.

        The child node starts empty; replay_at() walks the parent chain,
        so the branch inherits every call recorded on its ancestors until
        a call recorded ON the branch shadows it (divergence mechanic).

        Args:
            tree_id: Tree to fork in
            parent_node_id: Node to fork from (the checkpoint)
            branch_name: Human-readable branch label

        Returns:
            dict with tree_id, node_id, parent_id, depth

        Raises:
            ValueError: unknown tree or parent node
        """
        self._ensure_tree_tables()
        with _db() as conn:
            prow = conn.execute(
                f"SELECT depth FROM {REPLAY_TREE_NODES} WHERE tree_id = ? AND node_id = ?",
                (tree_id, parent_node_id),
            ).fetchone()
            if prow is None:
                raise ValueError(f"Unknown parent node {parent_node_id!r} in tree {tree_id!r}")
            node_id = uuid.uuid4().hex[:16]
            depth = int(prow["depth"]) + 1
            conn.execute(
                f"INSERT INTO {REPLAY_TREE_NODES} "
                f"(tree_id, node_id, parent_id, branch_name, depth, created_at) "
                f"VALUES (?, ?, ?, ?, ?, ?)",
                (tree_id, node_id, parent_node_id, branch_name, depth, time.time()),
            )
        return {
            "tree_id": tree_id,
            "node_id": node_id,
            "parent_id": parent_node_id,
            "depth": depth,
            "branch_name": branch_name,
        }

    def record_call_at(
        self, tree_id: str, node_id: str, tool_name: str, args: dict, response: Any
    ) -> dict:
        """Record a tool call ON a specific tree node (not the flat table).

        Recording on a forked node shadows the same call recorded on an
        ancestor — replay_at() prefers the deepest match.
        """
        self._ensure_tree_tables()
        args_json = normalize_tool_args(args)
        args_hash = _make_hash(tool_name, args)
        with _db() as conn:
            row = conn.execute(
                f"SELECT COALESCE(MAX(call_index), -1) + 1 as next_idx "
                f"FROM {REPLAY_TREE_CALLS} WHERE tree_id = ? AND node_id = ?",
                (tree_id, node_id),
            ).fetchone()
            call_index = int(row["next_idx"]) if row else 0
            conn.execute(
                f"INSERT INTO {REPLAY_TREE_CALLS} "
                f"(tree_id, node_id, call_index, tool_name, args_hash, args_json, "
                f"response_json, recorded_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    tree_id,
                    node_id,
                    call_index,
                    tool_name,
                    args_hash,
                    args_json,
                    json.dumps(response, default=str),
                    time.time(),
                ),
            )
        return {
            "tree_id": tree_id,
            "node_id": node_id,
            "call_index": call_index,
            "args_hash": args_hash,
        }

    def replay_at(self, tree_id: str, node_id: str, tool_name: str, args: dict) -> Optional[Any]:
        """Replay a tool call as of a tree node.

        Walks the node chain child → root and returns the DEEPEST recorded
        match for (tool_name, args_hash). This is the fork mechanic: a
        branch reuses its ancestors' recorded prefix and replays only its
        own divergence calls — without re-executing the shared prefix.

        Returns None when no node on the chain recorded this call.
        """
        self._ensure_tree_tables()
        args_hash = _make_hash(tool_name, args)
        with _db() as conn:
            chain = conn.execute(
                f"""
                WITH RECURSIVE chain(node_id, hop) AS (
                    SELECT node_id, 0 FROM {REPLAY_TREE_NODES}
                    WHERE tree_id = ? AND node_id = ?
                    UNION ALL
                    SELECT n.parent_id, c.hop + 1 FROM {REPLAY_TREE_NODES} n
                    JOIN chain c ON n.node_id = c.node_id AND n.tree_id = ?
                    WHERE n.parent_id IS NOT NULL
                )
                SELECT c.response_json
                FROM chain ch
                JOIN {REPLAY_TREE_CALLS} c
                  ON c.tree_id = ? AND c.node_id = ch.node_id
                 AND c.tool_name = ? AND c.args_hash = ?
                ORDER BY ch.hop ASC, c.call_index DESC
                LIMIT 1
                """,
                (tree_id, node_id, tree_id, tree_id, tool_name, args_hash),
            ).fetchone()
            if chain is not None:
                return json.loads(chain["response_json"])
        return None

    def get_tree(self, tree_id: str) -> dict:
        """Get tree metadata + all nodes (newest first)."""
        self._ensure_tree_tables()
        with _db() as conn:
            nodes = conn.execute(
                f"SELECT node_id, parent_id, branch_name, depth, created_at "
                f"FROM {REPLAY_TREE_NODES} WHERE tree_id = ? "
                f"ORDER BY depth DESC, created_at",
                (tree_id,),
            ).fetchall()
        if not nodes:
            raise ValueError(f"Unknown tree {tree_id!r}")
        return {
            "tree_id": tree_id,
            "nodes": [
                {
                    "node_id": n["node_id"],
                    "parent_id": n["parent_id"],
                    "branch_name": n["branch_name"],
                    "depth": n["depth"],
                    "created_at": n["created_at"],
                }
                for n in nodes
            ],
        }

    def list_nodes(self, tree_id: str, node_id: str) -> list[dict]:
        """List the ancestor chain of a node, root first."""
        self._ensure_tree_tables()
        with _db() as conn:
            rows = conn.execute(
                f"""
                WITH RECURSIVE chain(node_id, parent_id, branch_name, depth) AS (
                    SELECT node_id, parent_id, branch_name, depth
                    FROM {REPLAY_TREE_NODES}
                    WHERE tree_id = ? AND node_id = ?
                    UNION ALL
                    SELECT n.node_id, n.parent_id, n.branch_name, n.depth
                    FROM {REPLAY_TREE_NODES} n
                    JOIN chain c ON n.node_id = c.parent_id AND n.tree_id = ?
                )
                SELECT node_id, parent_id, branch_name, depth FROM chain
                ORDER BY depth
                """,
                (tree_id, node_id, tree_id),
            ).fetchall()
        return [
            {
                "node_id": r["node_id"],
                "parent_id": r["parent_id"],
                "branch_name": r["branch_name"],
                "depth": r["depth"],
            }
            for r in rows
        ]

    def node_call_count(self, tree_id: str, node_id: str) -> int:
        """Number of calls recorded directly on a node (not inherited)."""
        self._ensure_tree_tables()
        with _db() as conn:
            row = conn.execute(
                f"SELECT COUNT(*) AS cnt FROM {REPLAY_TREE_CALLS} "
                f"WHERE tree_id = ? AND node_id = ?",
                (tree_id, node_id),
            ).fetchone()
            return int(row["cnt"]) if row else 0

    def delete_tree(self, tree_id: str) -> dict:
        """Delete a tree and all its nodes and calls. Returns counts."""
        self._ensure_tree_tables()
        with _db() as conn:
            n_nodes = conn.execute(
                f"DELETE FROM {REPLAY_TREE_NODES} WHERE tree_id = ?", (tree_id,)
            ).rowcount
            n_calls = conn.execute(
                f"DELETE FROM {REPLAY_TREE_CALLS} WHERE tree_id = ?", (tree_id,)
            ).rowcount
        return {"nodes": n_nodes, "calls": n_calls}

    def list_trees(self) -> list[dict]:
        """List all trees with node/call counts."""
        self._ensure_tree_tables()
        with _db() as conn:
            rows = conn.execute(
                f"SELECT n.tree_id, MIN(n.created_at) AS created_at, "
                f"COUNT(DISTINCT n.node_id) AS nodes, "
                f"(SELECT COUNT(*) FROM {REPLAY_TREE_CALLS} c "
                f" WHERE c.tree_id = n.tree_id) AS calls "
                f"FROM {REPLAY_TREE_NODES} n GROUP BY n.tree_id ORDER BY created_at"
            ).fetchall()
        return [
            {
                "tree_id": r["tree_id"],
                "created_at": r["created_at"],
                "nodes": r["nodes"],
                "calls": r["calls"],
            }
            for r in rows
        ]


# ─── Convenience functions ─────────────────────────────────────────────────────


# ─── Scoped mode registry (G4: per-branch session isolation) ──────────────────
#
# The legacy _active singleton allows exactly ONE record/replay session per
# process: recording scenario A while replaying B is impossible, and
# concurrent tree branches (GEPA batch evaluation) would fight over one
# global mode. _scopes keys independent _Mode instances by scope id so each
# branch drives its own mode. scope=None always resolves to the legacy
# singleton — existing callers are byte-for-byte unaffected.

_scopes: dict[str, _Mode] = {}


def _scope_mode(scope: Optional[str]) -> _Mode:
    """Return the _Mode for a scope, creating it on first use.

    scope=None returns the legacy module singleton.
    """
    if scope is None:
        return _active
    mode = _scopes.get(scope)
    if mode is None:
        mode = _Mode()
        _scopes[scope] = mode
    return mode


def start_recording(scenario: str, scope: Optional[str] = None) -> dict:
    """Start recording mode for a scenario.

    Args:
        scenario: Scenario name — every tool call through the daemon is recorded.
        scope: Optional scope id (e.g. a tree branch). When set, this recording
            session lives in a scope-isolated _Mode instead of the legacy
            module singleton, so concurrent sessions don't fight over one
            global record/replay state.

    Returns:
        dict with status and scenario name
    """
    _scope_mode(scope).start(scenario, "record")
    return {"replay_mode": "record", "scenario": scenario, "scope": scope}


def start_replay(scenario: str, scope: Optional[str] = None, node_id: Optional[str] = None) -> dict:
    """Start replay mode for a scenario.

    Tool calls matching recorded responses will be served from cache
    without executing anything.

    Args:
        scenario: Scenario name to replay from.
        scope: Optional scope id for scope-isolated replay state.
        node_id: Optional tree node id — informational in the returned dict;
            tree-mode replay is selected per-call via intercept_call(tree_id=...).

    Returns:
        dict with status and scenario name
    """
    _scope_mode(scope).start(scenario, "replay")
    return {
        "replay_mode": "replay",
        "scenario": scenario,
        "scope": scope,
        "node_id": node_id,
    }


def stop(scope: Optional[str] = None) -> dict:
    """Stop Replay mode (both recording and replaying).

    Args:
        scope: Stop only this scope's mode. None stops the legacy singleton.
            A stopped scope is removed from the registry.

    Returns:
        dict with previous mode and scenario, or message if inactive
    """
    mode = _scope_mode(scope)
    prev = {"mode": mode.mode, "scenario": mode.scenario}
    mode.stop()
    if scope is not None and mode is not _active:
        _scopes.pop(scope, None)
    if prev["mode"] is None:
        return {"replay_mode": "inactive", "message": "Replay mode was not active"}
    return {"replay_mode": "stopped", "was": prev["mode"], "scenario": prev["scenario"]}


def status(scope: Optional[str] = None) -> dict:
    """Get current Replay mode status.

    Args:
        scope: Report this scope's status; None reports the legacy singleton.

    Returns:
        dict with mode, scenario, and is_active
    """
    mode = _scope_mode(scope)
    return {
        "replay_mode": mode.mode or "inactive",
        "scenario": mode.scenario,
        "is_active": mode.is_active(),
        "is_recording": mode.is_recording(),
        "is_replaying": mode.is_replaying(),
        "scope": scope,
    }


def intercept_call(
    tool_name: str,
    args: dict,
    scope: Optional[str] = None,
    tree_id: Optional[str] = None,
    node_id: Optional[str] = None,
) -> Optional[dict]:
    """Intercept a tool call for Recording/replay.

    Called by the daemon before executing a tool. If replay is active:
    - Replay mode: returns cached response if found, or None (miss)
    - Record mode: returns None (let it execute), the response is recorded
      by the caller via record_response()

    Scope/tree awareness (G4): scope selects an isolated mode state so
    concurrent branches (e.g. GEPA batch evaluation) don't share one global
    record/replay session. With tree_id+node_id set in replay mode, the
    response resolves through the tree node chain via replay_at() (deepest
    node match wins) instead of the flat scenario table.

    This is the integration point between the daemon and Replay mode.

    Args:
        tool_name: The tool being called
        args: The tool arguments
        scope: Scope-isolated mode state (None = legacy singleton)
        tree_id: Replay tree id (replay mode with tree resolution)
        node_id: Tree node to replay as of

    Returns:
        Dict with {"replay_hit": True, "data": ...} on replay hit,
        or None if no replay action needed (normal execution)
    """
    mode = _scope_mode(scope)
    if not mode.is_active():
        return None

    manager = ReplayManager()

    if mode.is_replaying():
        scenario = mode.scenario
        assert scenario is not None
        if tree_id is not None and node_id is not None:
            response = manager.replay_at(tree_id, node_id, tool_name, args)
        else:
            response = manager.find_replay(scenario, tool_name, args)
        if response is not None:
            return {"replay_hit": True, "data": response}
        return {"replay_hit": False, "message": f"No replay found for {tool_name}"}

    if mode.is_recording():
        return {"replay_hit": False, "message": "recording"}

    return None


def record_response(
    tool_name: str,
    args: dict,
    response: Any,
    scenario: Optional[str] | None = None,
    scope: Optional[str] = None,
    tree_id: Optional[str] = None,
    node_id: Optional[str] = None,
) -> dict:
    """Record a tool response after real execution.

    Called by the daemon after a tool executes successfully while replay
    recording is active.

    Args:
        tool_name: The tool that was called
        args: The tool arguments
        response: The response to record
        scenario: Override scenario name (uses active scenario by default)
        scope: Scope-isolated recording state (None = legacy singleton)
        tree_id: With node_id, record ON this tree node via record_call_at
        node_id: Tree node to record on

    Returns:
        dict with recording result
    """
    mode = _scope_mode(scope)
    if not mode.is_recording():
        return {"replay_mode": "not_recording"}

    if tree_id is not None and node_id is not None:
        manager = ReplayManager()
        result = manager.record_call_at(tree_id, node_id, tool_name, args, response)
        return {"replay_mode": "record", "tree": result}

    actual_scenario = scenario or mode.scenario
    if actual_scenario is None:
        return {"replay_mode": "error", "message": "No active scenario"}

    manager = ReplayManager()
    return manager.record_call(actual_scenario, tool_name, args, response)
