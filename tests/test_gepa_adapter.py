"""Tests for the GEPA adapter (toolrecall.adapters.gepa).

Framework-free (structural protocol, no gepa import): exercises the fork-tree
rollout mechanic, candidate chaining, and the reflective dataset against the
hermetic per-test cache DB (conftest.py). The task functions record/replay
through intercept_tool_call/record_tool_response exactly like a real agent
loop would.
"""

import pytest


@pytest.fixture(autouse=True)
def _reset_db_singleton(_isolated_toolrecall_env):
    """Make conftest's per-test TOOLRECALL_CACHE_DB actually take effect."""
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


@pytest.fixture()
def fresh_adapter():
    """Adapter with a private tree, cleaned up after the test."""
    from toolrecall.adapters.gepa import ToolRecallGepaAdapter

    made = []

    def make(**kwargs):
        a = ToolRecallGepaAdapter(**kwargs)
        made.append(a)
        return a

    yield make
    for a in made:
        a.cleanup()


# ---- the representative agent task --------------------------------------------


class TestProtocolShape:
    def test_no_gepa_import(self):
        """Zero-dep identity: the adapter source must not import gepa."""
        import inspect

        import toolrecall.adapters.gepa as g

        src = open(inspect.getsourcefile(g), encoding="utf-8").read()
        assert "import gepa\n" not in src
        assert "from gepa" not in src

    def test_evaluate_returns_gepa_batch_shape(self, fresh_adapter):

        calls: list[str] = []

        def task(ti, ctx):
            calls.append(ti)
            return f"out:{ti}"

        a = fresh_adapter(task_input_fn=task, scorer=lambda ti, out: 1.0)
        batch = a.evaluate(["x", "y"], {"sys": "p1"}, capture_traces=True)
        assert {"outputs", "scores", "trajectories"} <= set(batch.keys())
        assert batch["outputs"] == ["out:x", "out:y"]
        assert batch["scores"] == [1.0, 1.0]
        assert batch["trajectories"] is not None and len(batch["trajectories"]) == 2
        assert batch["trajectories"][0]["branch_ctx"]["tree_id"] == a.tree_id

    def test_trajectories_none_without_capture(self, fresh_adapter):
        def task(ti, ctx):
            return "o"

        a = fresh_adapter(task_input_fn=task, scorer=lambda ti, o: 1.0)
        batch = a.evaluate(["x"], {"sys": "p"})
        assert batch["trajectories"] is None

    def test_failed_rollout_scores_zero_never_raises(self, fresh_adapter):
        def task(ti, ctx):
            if ti == "boom":
                raise RuntimeError("kaboom")
            return "ok"

        a = fresh_adapter(task_input_fn=task, scorer=lambda ti, o: 1.0)
        batch = a.evaluate(["boom", "fine"], {"sys": "p"})
        assert batch["scores"] == [0.0, 1.0]
        assert batch["outputs"][0] == {"error": "kaboom"}

    def test_reflective_dataset_shape(self, fresh_adapter):
        def task(ti, ctx):
            return f"out:{ti}"

        a = fresh_adapter(task_input_fn=task, scorer=lambda ti, o: 0.5)
        eb = a.evaluate(["x"], {"sys": "p"})
        ds = a.make_reflective_dataset({"sys": "p"}, eb, ["sys"])
        assert set(ds) == {"sys"}
        rec = ds["sys"][0]
        assert set(rec) >= {"Inputs", "Generated Outputs", "Feedback"}
        assert "Incorrect" in rec["Feedback"]


# ---- the fork mechanic --------------------------------------------------------


class TestForkMechanic:
    def test_candidate2_replays_shared_prefix(self, fresh_adapter):
        """Candidate 2 must EXECUTE strictly fewer tool calls than candidate 1."""

        executions: list[str] = []

        def task(ti, ctx):
            from toolrecall.adapters.gepa import intercept_tool_call, record_tool_response

            def tool_call(name, args):
                hit = intercept_tool_call(name, args, ctx["tree_id"], ctx["node_id"])
                if hit["replay_hit"]:
                    executions.append(f"R:{args['path']}")
                    return hit["data"]
                resp = f"CONTENT-{args['path']}"
                record_tool_response(name, args, resp, ctx["tree_id"], ctx["node_id"])
                executions.append(f"E:{args['path']}")
                return resp

            shared = tool_call("cached_read", {"path": "/shared/a.txt"})
            if ti == "divergent":
                _ = tool_call("cached_read", {"path": "/div/b.txt"})
            return f"done:{shared}"

        a = fresh_adapter(task_input_fn=task, scorer=lambda ti, o: 1.0)
        c1 = a.evaluate(["plain", "divergent"], {"sys": "v1"})
        exec_c1 = sum(1 for e in executions if e.startswith("E:"))
        replay_c1 = sum(1 for e in executions if e.startswith("R:"))
        # First candidate: everything real (2 execs), nothing replayed
        assert exec_c1 == 3 and replay_c1 == 0, (exec_c1, replay_c1)

        executions.clear()
        c2 = a.evaluate(["plain", "divergent"], {"sys": "v2"})
        exec_c2 = sum(1 for e in executions if e.startswith("E:"))
        replay_c2 = sum(1 for e in executions if e.startswith("R:"))
        # Same batch, chained candidates: BOTH shared calls replay from the
        # parent chain (input 2's shared call lives on its OWN candidate-1
        # branch, position 0's call on position 0's branch); the divergent
        # call also replays — it was recorded by candidate 1 at the same
        # chain position, and candidate 2's args match. Zero re-execution
        # when behavior didn't change on this branch.
        assert replay_c2 == 3, f"full prefix should replay: {executions}"
        assert exec_c2 == 0, f"nothing should execute: {executions}"
        # Scores identical — replay is transparent
        assert c1["scores"] == c2["scores"] == [1.0, 1.0]
        # outputs identical through replay
        assert c2["outputs"] == c1["outputs"]

    def test_branch_tree_shape(self, fresh_adapter):
        """Chained candidates form a per-position chain in the tree."""

        def task(ti, ctx):
            return "o"

        a = fresh_adapter(task_input_fn=task, scorer=lambda ti, o: 1.0)
        b1 = a.evaluate(["t1", "t2"], {"sys": "v1"})
        b2 = a.evaluate(["t1", "t2"], {"sys": "v2"})
        # candidate 2's branch for position 0 hangs off candidate 1's
        assert b2["branch_ctxs"][0]["parent_id"] == b1["branch_ctxs"][0]["node_id"]
        assert b2["branch_ctxs"][1]["node_id"] != b2["branch_ctxs"][0]["node_id"]
        assert a.tree_summary()["nodes"] == 5  # root + 2 + 2

    def test_no_chain_forks_from_root(self, fresh_adapter):
        """chain=False: every candidate forks the root (record-mode arm)."""

        def task(ti, ctx):
            return "o"

        a = fresh_adapter(task_input_fn=task, scorer=lambda ti, o: 1.0, chain=False)
        b1 = a.evaluate(["t"], {"sys": "v1"})
        b2 = a.evaluate(["t"], {"sys": "v2"})
        assert b2["branch_ctxs"][0]["parent_id"] == a.root_node_id
        assert b1["branch_ctxs"][0]["parent_id"] == a.root_node_id

    def test_rollout_stats(self, fresh_adapter):

        def task(ti, ctx):
            return "o"

        a = fresh_adapter(task_input_fn=task, scorer=lambda ti, o: 1.0)
        batch = a.evaluate(["x"], {"sys": "p"})
        r = batch["rollouts"][0]
        assert r.score == 1.0 and r.duration_s >= 0
        assert r.tool_calls == 0  # task used no interception
        assert r.tree_id == a.tree_id


# ---- isolation ----------------------------------------------------------------


class TestIsolation:
    def test_trees_are_private_between_adapters(self, fresh_adapter):

        def task(ti, ctx):
            return "o"

        a1 = fresh_adapter(task_input_fn=task, scorer=lambda ti, o: 1.0)
        a2 = fresh_adapter(task_input_fn=task, scorer=lambda ti, o: 1.0)
        a1.evaluate(["x"], {"sys": "p"})
        a2.evaluate(["x"], {"sys": "p"})
        assert a1.tree_id != a2.tree_id
        assert a1.tree_summary()["nodes"] == 2
        assert a2.tree_summary()["nodes"] == 2

    def test_legacy_singleton_untouched(self, fresh_adapter):
        import toolrecall.replay as R

        def task(ti, ctx):
            return "o"

        a = fresh_adapter(task_input_fn=task, scorer=lambda ti, o: 1.0)
        a.evaluate(["x"], {"sys": "p"})
        assert R.status()["replay_mode"] == "inactive"

    def test_intercept_direct_api(self):
        """intercept_tool_call/record_tool_response work without the adapter."""
        from toolrecall.adapters.gepa import intercept_tool_call, record_tool_response
        from toolrecall.replay import ReplayManager

        m = ReplayManager()
        tree = m.fork_tree("direct")
        tid, root = tree["tree_id"], tree["root"]
        assert intercept_tool_call("t", {"a": 1}, tid, root) == {"replay_hit": False}
        record_tool_response("t", {"a": 1}, "VAL", tid, root)
        assert intercept_tool_call("t", {"a": 1}, tid, root) == {"replay_hit": True, "data": "VAL"}
        m.delete_tree(tid)

    def test_inherited_calls_introspection(self, fresh_adapter):

        def task(ti, ctx):
            from toolrecall.adapters.gepa import intercept_tool_call, record_tool_response

            hit = intercept_tool_call("t", {"n": ti}, ctx["tree_id"], ctx["node_id"])
            if hit["replay_hit"]:
                return hit["data"]
            record_tool_response("t", {"n": ti}, f"val-{ti}", ctx["tree_id"], ctx["node_id"])
            return f"val:{ti}"

        a = fresh_adapter(task_input_fn=task, scorer=lambda ti, o: 1.0)
        a.evaluate(["a", "b"], {"sys": "v1"})
        # position 0's node has 1 own call; candidate 2 inherits it
        b2 = a.evaluate(["a", "b"], {"sys": "v2"})
        ctx2 = b2["branch_ctxs"][0]
        assert a.inherited_calls(ctx2["node_id"]) >= 1
        assert b2["rollouts"][0].node_id == ctx2["node_id"]
