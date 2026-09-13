# Copyright (c) 2026 Robin Schultka
# SPDX-License-Identifier: MIT
# Source: https://github.com/whiskybeer/toolrecall
"""
toolrecall.adapters.gepa — GEPA ``evaluate`` adapter over the replay fork tree.

Bridges GEPA-style prompt optimizers (gepa-ai/gepa) to ToolRecall's fork
primitives (``toolrecall.replay``): the first candidate's rollout records its
tool calls ON tree branches; every later candidate forks from the previous
candidate's same-position branch and resolves intercepted calls through the
node chain — the shared tool-call prefix replays from ancestors, only
divergent calls (different args → different hash → tree miss) re-execute.

The adapter implements the ``GEPAAdapter`` protocol STRUCTURALLY (duck-typed;
an actual gepa import appears nowhere and this file is zero-dep stdlib —
gepa is opt-in client-side, matching the LangGraph adapter pattern and the
zero-runtime-deps identity rule). Protocol surface:

* ``evaluate(batch, candidate, capture_traces)`` → outputs/scores/trajectories
* ``make_reflective_dataset(candidate, eval_batch, components_to_update)``

Isolation note (G4): tree rollouts need no module-level mode state — every
call is keyed by (tree_id, node_id), so concurrent candidates cannot
cross-contaminate by construction. The scope-isolated _Mode registry in
``toolrecall.replay`` covers the FLAT scenario path; the tree path is
stateless per-node. The adapter never touches the legacy singleton.

Integration shape (HANDOVER step 4):

    adapter = ToolRecallGepaAdapter(
        task_input_fn=my_task_function,   # routes tool calls through
                                          #   intercept_tool_call/record_tool_response
        scorer=my_score_function,         # (task_input, output) -> float
    )
    # gepa.optimize(..., evaluate=adapter.evaluate) — or any compatible loop

Naive-arm contract for the A/B bench (HANDOVER step 5): run the same
``task_input_fn`` WITHOUT interception — the harness measures real
execution cost per candidate. The fork arm replays the shared prefix and
re-executes only divergence.
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any, Callable

from toolrecall import replay as _replay

__all__ = [
    "ToolRecallGepaAdapter",
    "intercept_tool_call",
    "record_tool_response",
    "RolloutResult",
    "EvalBatchResult",
]

_MANAGER = _replay.ReplayManager()


class EvalBatchResult:
    """EvaluationBatch-shaped result: attribute access for the REAL gepa
    engine (``.outputs``, ``.scores``, ``.trajectories``, ``.objective_scores``,
    ``.num_metric_calls``) plus dict-style ``result["outputs"]`` / ``.get()``
    for our own tests and harness. Extra ToolRecall fields ride as plain
    attributes (``branch_ctxs``, ``rollouts``) — the engine ignores them.
    """

    __slots__ = (
        "outputs",
        "scores",
        "trajectories",
        "objective_scores",
        "num_metric_calls",
        "branch_ctxs",
        "rollouts",
    )

    def __init__(
        self,
        outputs: list[Any],
        scores: list[float],
        trajectories: list[dict] | None,
        objective_scores: list[dict[str, float]] | None = None,
        num_metric_calls: int | None = None,
        branch_ctxs: list[dict] | None = None,
        rollouts: list[RolloutResult] | None = None,
    ):
        self.outputs = outputs
        self.scores = scores
        self.trajectories = trajectories
        self.objective_scores = objective_scores
        self.num_metric_calls = num_metric_calls
        self.branch_ctxs = branch_ctxs if branch_ctxs is not None else []
        self.rollouts = rollouts if rollouts is not None else []

    # dict-style access for our tests/harness
    _KEYS = {
        "outputs": "outputs",
        "scores": "scores",
        "trajectories": "trajectories",
        "objective_scores": "objective_scores",
        "num_metric_calls": "num_metric_calls",
        "branch_ctxs": "branch_ctxs",
        "rollouts": "rollouts",
    }

    def __getitem__(self, key: str) -> Any:
        if key in self._KEYS:
            return getattr(self, key)
        raise KeyError(key)

    def get(self, key: str, default: Any = None) -> Any:
        if key in self._KEYS:
            return getattr(self, key)
        return default

    def __contains__(self, key: str) -> bool:
        return key in self._KEYS

    def keys(self):
        return self._KEYS.keys()


def intercept_tool_call(tool_name: str, args: dict, tree_id: str, node_id: str) -> dict:
    """Resolve a tool call against the fork-tree node chain (stateless).

    Returns ``{"replay_hit": True, "data": ...}`` when the chain (node and
    its ancestors, deepest match wins) has a recorded response; otherwise
    ``{"replay_hit": False}`` — the caller executes for real and calls
    :func:`record_tool_response`.
    """
    data = _MANAGER.replay_at(tree_id, node_id, tool_name, args)
    if data is not None:
        return {"replay_hit": True, "data": data}
    return {"replay_hit": False}


def record_tool_response(
    tool_name: str, args: dict, response: Any, tree_id: str, node_id: str
) -> dict:
    """Record a tool response ON a tree node after real execution.

    Recording on the node shadows the same call recorded on ancestors for
    all future replays that resolve through this node.
    """
    return _MANAGER.record_call_at(tree_id, node_id, tool_name, args, response)


class RolloutResult:
    """What one rollout produced — the unit the optimizer loop consumes."""

    __slots__ = (
        "output",
        "score",
        "task_input",
        "node_id",
        "tree_id",
        "tool_calls",
        "duration_s",
    )

    def __init__(
        self,
        output: Any,
        score: float,
        task_input: Any,
        tree_id: str,
        node_id: str,
        tool_calls: int,
        duration_s: float,
    ):
        self.output = output
        self.score = score
        self.task_input = task_input
        self.tree_id = tree_id
        self.node_id = node_id
        self.tool_calls = tool_calls
        self.duration_s = duration_s


class ToolRecallGepaAdapter:
    """GEPA-compatible evaluator whose rollouts run against the fork tree.

    Chaining (the fork mechanic): batch element *i* of candidate *k* forks
    from candidate *k−1*'s element-*i* node, so each rollout inherits every
    tool call its predecessor recorded at that position and re-executes only
    what its (changed prompt) behavior diverged on. The first candidate
    forks from the tree root and records everything for real.

    Parameters
    ----------
    task_input_fn:
        ``(task_input, branch_ctx) -> output``. Inside, route every tool
        call through :func:`intercept_tool_call` / :func:`record_tool_response`
        with ``branch_ctx["tree_id"]`` / ``branch_ctx["node_id"]``.
    scorer:
        ``(task_input, output) -> float`` — higher is better.
    tree_id:
        Existing tree to fork into; None → created lazily and reused.
    chain:
        True (default) = candidates chain per position (fork mechanic on).
        False = every candidate forks from the root (pure record mode —
        the comparison arm measures against this).
    branch_prefix:
        Label prefix for branch names.
    """

    def __init__(
        self,
        task_input_fn: Callable[[Any, dict], Any],
        scorer: Callable[[Any, Any], float],
        tree_id: str | None = None,
        chain: bool = True,
        branch_prefix: str = "cand",
    ):
        self.task_input_fn = task_input_fn
        self.scorer = scorer
        self._tree_id = tree_id
        self._chain = chain
        self._prefix = branch_prefix
        self._branch_seq = 0
        self._position_parents: dict[int, str] = {}
        self._last_results: list[RolloutResult] = []

    # ── tree bootstrap ────────────────────────────────────────────────────

    @property
    def tree_id(self) -> str:
        """Tree id — created on first access, reused afterwards."""
        if self._tree_id is None:
            tree = _MANAGER.fork_tree(f"gepa-{uuid.uuid4().hex[:8]}")
            self._tree_id = tree["tree_id"]
        return self._tree_id

    @property
    def root_node_id(self) -> str:
        """The tree's root node."""
        nodes = _MANAGER.get_tree(self.tree_id)["nodes"]
        for n in nodes:
            if n["depth"] == 0:
                return n["node_id"]
        raise ValueError("Tree has no root node")  # pragma: no cover — fork_tree always makes one

    def fork_branch(self, label: str, parent_node_id: str) -> dict:
        """Fork one branch under ``parent_node_id``; returns the branch ctx."""
        node = _MANAGER.fork_node(self.tree_id, parent_node_id, label)
        self._branch_seq += 1
        return {
            "tree_id": self.tree_id,
            "node_id": node["node_id"],
            "parent_id": parent_node_id,
            "depth": node["depth"],
            "label": label,
            "candidate_branch_seq": self._branch_seq,
        }

    # ── GEPAAdapter protocol (structural) ─────────────────────────────────

    def evaluate(
        self,
        batch: list[Any],
        candidate: dict[str, str],
        capture_traces: bool = False,
    ) -> dict:
        """Evaluate a candidate on a batch — one fork-tree branch per input.

        Matches GEPA's EvaluationBatch shape (duck-typed dict): ``outputs``,
        ``scores``, ``trajectories`` (None unless capture_traces), plus
        ToolRecall extras ``branch_ctxs`` and ``rollouts`` for tests and the
        A/B harness.

        A failed rollout becomes score 0.0 with an ``{"error": ...}``
        output — a crashed candidate is bad, not fatal (GEPA best
        practice); the optimizer loop never sees an exception from here.
        """
        outputs: list[Any] = []
        scores: list[float] = []
        trajectories: list[dict] | None = [] if capture_traces else None
        branch_ctxs: list[dict] = []
        rollouts: list[RolloutResult] = []
        new_parents: dict[int, str] = {}

        for i, task_input in enumerate(batch):
            parent = (self._position_parents.get(i) if self._chain else None) or self.root_node_id
            label = f"{self._prefix}-{self._branch_seq}"
            branch_ctx = self.fork_branch(label, parent)
            branch_ctx["candidate"] = dict(candidate)
            new_parents[i] = branch_ctx["node_id"]
            t0 = time.time()
            calls_before = _MANAGER.node_call_count(branch_ctx["tree_id"], branch_ctx["node_id"])
            try:
                output = self.task_input_fn(task_input, branch_ctx)
                score = float(self.scorer(task_input, output))
            except Exception as e:  # noqa: BLE001 — failed rollout = score 0, never raise
                output = {"error": str(e)}
                score = 0.0
            duration = time.time() - t0
            tool_calls = (
                _MANAGER.node_call_count(branch_ctx["tree_id"], branch_ctx["node_id"])
                - calls_before
            )

            rollouts.append(
                RolloutResult(
                    output=output,
                    score=score,
                    task_input=_safe_json(task_input),
                    tree_id=branch_ctx["tree_id"],
                    node_id=branch_ctx["node_id"],
                    tool_calls=tool_calls,
                    duration_s=duration,
                )
            )
            outputs.append(output)
            scores.append(score)
            branch_ctxs.append(branch_ctx)
            if capture_traces:
                assert trajectories is not None  # narrowed: [] when capture_traces
                trajectories.append(
                    {
                        "task_input": _safe_json(task_input),
                        "output": _safe_json(output),
                        "score": score,
                        "branch_ctx": branch_ctx,
                        "tool_calls": tool_calls,
                        "duration_s": duration,
                    }
                )

        # Chained: the NEXT candidate forks from THIS candidate's nodes.
        self._position_parents = new_parents
        self._last_results = rollouts
        return EvalBatchResult(
            outputs=outputs,
            scores=scores,
            trajectories=trajectories,
            objective_scores=None,
            num_metric_calls=len(batch),
            branch_ctxs=branch_ctxs,
            rollouts=rollouts,
        )

    def make_reflective_dataset(
        self,
        candidate: dict[str, str],
        eval_batch: dict,
        components_to_update: list[str],
    ) -> dict[str, list[dict]]:
        """Build the per-component reflective dataset from the last rollouts.

        GEPA passes back the EvaluationBatch returned by evaluate() — here a
        dict carrying ``rollouts``. Feedback is deterministic from
        (input, output, score).
        """
        rollouts = eval_batch.get("rollouts") or self._last_results
        dataset: dict[str, list[dict]] = {}
        for component in components_to_update:
            records: list[dict] = []
            for r in rollouts:
                records.append(
                    {
                        "Inputs": {
                            "component": component,
                            "task_input": _safe_json(r.task_input),
                        },
                        "Generated Outputs": {"output": _safe_json(r.output)},
                        "Feedback": _feedback(r.score, r.output),
                        "tool_calls_recorded": r.tool_calls,
                        "node_id": r.node_id,
                    }
                )
            dataset[component] = records
        return dataset

    def propose_new_texts(
        self,
        candidate: dict[str, str],
        reflective_dataset: dict[str, list[dict]],
        components_to_update: list[str],
    ) -> dict[str, str]:
        """Adapter-owned deterministic proposal — lets the REAL gepa engine
        run this adapter with zero LLM API calls.

        Deterministic mutation driven by the reflective Feedback records:
        - failed records present → append ' [greedy]' once (changes task
          behavior downstream: the e2e task reads an extra file).
        - otherwise append ' [v2]' (idempotent marker, no behavior change).
        A real deployment replaces this with an LLM-backed proposer; the
        fork-tree mechanic is orthogonal to who proposes.
        """
        new_texts: dict[str, str] = {}
        for component in components_to_update:
            records = reflective_dataset.get(component) or []
            failed = sum(1 for rec in records if "Incorrect" in str(rec.get("Feedback", "")))
            current = candidate.get(component, "")
            if failed and "[greedy]" not in current:
                new_texts[component] = current.rstrip() + " [greedy]"
            elif "[v2]" not in current:
                new_texts[component] = current.rstrip() + " [v2]"
            else:
                new_texts[component] = current
        return new_texts

    # ── introspection / teardown ──────────────────────────────────────────

    def tree_summary(self) -> dict:
        """Node/call counts of this adapter's tree (for the A/B harness)."""
        for t in _MANAGER.list_trees():
            if t["tree_id"] == self.tree_id:
                return {"tree_id": t["tree_id"], "nodes": t["nodes"], "calls": t["calls"]}
        return {"tree_id": self.tree_id, "nodes": 0, "calls": 0}

    def inherited_calls(self, node_id: str) -> int:
        """Calls this node can replay from its whole chain (no re-execution)."""
        total = 0
        for n in _MANAGER.list_nodes(self.tree_id, node_id):
            total += _MANAGER.node_call_count(self.tree_id, n["node_id"])
        return total

    def cleanup(self) -> None:
        """Delete this adapter's tree (test/bench teardown)."""
        if self._tree_id is None:
            return
        try:
            _MANAGER.delete_tree(self._tree_id)
        except (ValueError, TypeError):
            pass


def _safe_json(obj: Any) -> Any:
    """JSON-safe deep copy for trajectories/records (stdlib only)."""
    try:
        return json.loads(json.dumps(obj, default=str))
    except Exception:
        return str(obj)


def _feedback(score: float, output: Any) -> str:
    """Deterministic reflective-dataset feedback from a rollout result."""
    if score >= 1.0:
        return "Correct."
    out_repr = str(output)
    if len(out_repr) > 300:
        out_repr = out_repr[:300] + "…"
    return f"Incorrect (score {score:.3f}). Output: {out_repr}"
