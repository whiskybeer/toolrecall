"""Tests for the replay tree / fork primitives (G1) and scope isolation (G4).

Framework-free: exercises ReplayManager tree methods, scoped mode functions,
and ContextTracker branch-scoped persistence against the hermetic per-test
cache DB (conftest.py), with daemon_running() mocked True so the code paths
match production interception flow.
"""

import pytest

import toolrecall.client as tr_client


@pytest.fixture(autouse=True)
def _reset_db_singleton(_isolated_toolrecall_env):
    """Make conftest's per-test TOOLRECALL_CACHE_DB actually take effect.

    toolrecall._db caches both config and the sqlite connection as module
    singletons; without this reset every test after the first writes to the
    FIRST test's temp DB. Test-infra only — no production code touched.
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
    # Also clear the scoped-mode registry between tests so a scope id from
    # one test never leaks into another.
    import toolrecall.replay as replay_mod

    replay_mod._scopes.clear()


@pytest.fixture()
def daemon_up(monkeypatch):
    monkeypatch.setattr(tr_client, "daemon_running", lambda: True)


# ---- G1: tree / fork primitives ----------------------------------------------


class TestTreePrimitives:
    def test_fork_tree_creates_root(self):
        from toolrecall.replay import ReplayManager

        m = ReplayManager()
        tree = m.fork_tree("sweep1")
        assert tree["tree_id"] and tree["root"] and tree["name"] == "sweep1"
        nodes = m.get_tree(tree["tree_id"])["nodes"]
        assert len(nodes) == 1
        assert nodes[0]["node_id"] == tree["root"]
        assert nodes[0]["parent_id"] is None
        assert nodes[0]["depth"] == 0

    def test_fork_node_increments_depth_and_links_parent(self):
        from toolrecall.replay import ReplayManager

        m = ReplayManager()
        tree = m.fork_tree("t")
        tid, root = tree["tree_id"], tree["root"]
        b = m.fork_node(tid, root, "branch-A")
        g = m.fork_node(tid, b["node_id"], "branch-A2")
        assert b["parent_id"] == root and b["depth"] == 1
        assert g["parent_id"] == b["node_id"] and g["depth"] == 2
        m.delete_tree(tid)

    def test_fork_node_unknown_parent_raises(self):
        from toolrecall.replay import ReplayManager

        m = ReplayManager()
        tree = m.fork_tree("t")
        with pytest.raises(ValueError, match="Unknown parent"):
            m.fork_node(tree["tree_id"], "nonexistent", "x")
        m.delete_tree(tree["tree_id"])

    def test_record_and_replay_at_inherits_shared_prefix(self):
        """Branch replays its ancestors' calls without recording them itself."""
        from toolrecall.replay import ReplayManager

        m = ReplayManager()
        tree = m.fork_tree("t")
        tid, root = tree["tree_id"], tree["root"]
        m.record_call_at(tid, root, "cached_read", {"path": "/a.txt"}, "SHARED")
        b1 = m.fork_node(tid, root, "branch-1")
        b2 = m.fork_node(tid, root, "branch-2")
        # Both branches inherit the root's shared-prefix call
        assert m.replay_at(tid, b1["node_id"], "cached_read", {"path": "/a.txt"}) == "SHARED"
        assert m.replay_at(tid, b2["node_id"], "cached_read", {"path": "/a.txt"}) == "SHARED"
        m.delete_tree(tid)

    def test_divergence_is_branch_local(self):
        """A call recorded on branch-1 is invisible to branch-2 and the root."""
        from toolrecall.replay import ReplayManager

        m = ReplayManager()
        tree = m.fork_tree("t")
        tid, root = tree["tree_id"], tree["root"]
        m.record_call_at(tid, root, "cached_read", {"path": "/a.txt"}, "SHARED")
        b1 = m.fork_node(tid, root, "branch-1")
        b2 = m.fork_node(tid, root, "branch-2")
        m.record_call_at(tid, b1["node_id"], "cached_write", {"path": "/b.txt"}, "B1")
        assert m.replay_at(tid, b1["node_id"], "cached_write", {"path": "/b.txt"}) == "B1"
        assert m.replay_at(tid, b2["node_id"], "cached_write", {"path": "/b.txt"}) is None
        assert m.replay_at(tid, root, "cached_write", {"path": "/b.txt"}) is None
        m.delete_tree(tid)

    def test_shadowing_deepest_node_wins(self):
        """Same (tool, args) recorded on root AND branch: branch view sees branch's."""
        from toolrecall.replay import ReplayManager

        m = ReplayManager()
        tree = m.fork_tree("t")
        tid, root = tree["tree_id"], tree["root"]
        m.record_call_at(tid, root, "cached_read", {"path": "/a.txt"}, "ROOT-VER")
        b1 = m.fork_node(tid, root, "branch-1")
        b2 = m.fork_node(tid, root, "branch-2")
        m.record_call_at(tid, b1["node_id"], "cached_read", {"path": "/a.txt"}, "B1-VER")
        # b1 shadows root's version; b2 still sees root's
        assert m.replay_at(tid, b1["node_id"], "cached_read", {"path": "/a.txt"}) == "B1-VER"
        assert m.replay_at(tid, b2["node_id"], "cached_read", {"path": "/a.txt"}) == "ROOT-VER"
        # root's own view unchanged
        assert m.replay_at(tid, root, "cached_read", {"path": "/a.txt"}) == "ROOT-VER"
        m.delete_tree(tid)

    def test_args_hashing_matches_flat_table_convention(self):
        """record_call_at must hash args exactly like the flat replay path."""
        from toolrecall.replay import ReplayManager

        m = ReplayManager()
        tree = m.fork_tree("t")
        tid, root = tree["tree_id"], tree["root"]
        m.record_call_at(tid, root, "cached_read", {"path": "/a.txt", "limit": 5}, "R")
        assert m.replay_at(tid, root, "cached_read", {"path": "/a.txt", "limit": 5}) == "R"
        # different args → different hash → miss
        assert m.replay_at(tid, root, "cached_read", {"path": "/a.txt", "limit": 9}) is None
        m.delete_tree(tid)

    def test_get_tree_unknown_raises(self):
        from toolrecall.replay import ReplayManager

        m = ReplayManager()
        with pytest.raises(ValueError, match="Unknown tree"):
            m.get_tree("nope")

    def test_list_nodes_returns_root_first_chain(self):
        from toolrecall.replay import ReplayManager

        m = ReplayManager()
        tree = m.fork_tree("t")
        tid, root = tree["tree_id"], tree["root"]
        b = m.fork_node(tid, root, "b1")
        g = m.fork_node(tid, b["node_id"], "b1-2")
        chain = m.list_nodes(tid, g["node_id"])
        assert [n["branch_name"] for n in chain] == ["t", "b1", "b1-2"]
        assert [n["depth"] for n in chain] == [0, 1, 2]
        m.delete_tree(tid)

    def test_delete_tree_removes_nodes_and_calls(self):
        from toolrecall.replay import ReplayManager

        m = ReplayManager()
        tree = m.fork_tree("t")
        tid, root = tree["tree_id"], tree["root"]
        m.record_call_at(tid, root, "cached_read", {"path": "/a"}, "x")
        b = m.fork_node(tid, root, "b")
        m.record_call_at(tid, b["node_id"], "cached_read", {"path": "/b"}, "y")
        result = m.delete_tree(tid)
        assert result["nodes"] == 2 and result["calls"] == 2
        assert m.list_trees() == []
        with pytest.raises(ValueError, match="Unknown tree"):
            m.get_tree(tid)

    def test_list_trees_reports_counts(self):
        from toolrecall.replay import ReplayManager

        m = ReplayManager()
        tree = m.fork_tree("t")
        tid, root = tree["tree_id"], tree["root"]
        m.record_call_at(tid, root, "a", {}, 1)
        m.record_call_at(tid, root, "b", {}, 2)
        trees = m.list_trees()
        assert len(trees) == 1
        assert trees[0]["nodes"] == 1 and trees[0]["calls"] == 2
        m.delete_tree(tid)


# ---- G4: scoped mode registry -------------------------------------------------


class TestScopedModes:
    def test_legacy_singleton_untouched_by_scopes(self):
        import toolrecall.replay as R

        R.start_recording("s1", scope="b1")
        R.record_response("t", {"p": 1}, "v1", scope="b1")
        assert R.status()["replay_mode"] == "inactive"
        assert R.status()["scope"] is None

    def test_scopes_record_independently(self):
        import toolrecall.replay as R

        R.start_recording("sa", scope="bra")
        R.start_recording("sb", scope="brb")
        R.record_response("t", {"p": 1}, "A", scope="bra")
        R.record_response("t", {"p": 1}, "B", scope="brb")
        # legacy flat replay resolves per-scope scenario
        R.start_replay("sa", scope="bra")
        R.start_replay("sb", scope="brb")
        hit_a = R.intercept_call("t", {"p": 1}, scope="bra")
        hit_b = R.intercept_call("t", {"p": 1}, scope="brb")
        assert hit_a == {"replay_hit": True, "data": "A"}
        assert hit_b == {"replay_hit": True, "data": "B"}
        R.stop(scope="bra")
        R.stop(scope="brb")

    def test_recording_while_other_scope_replays(self):
        """The case the legacy singleton made impossible."""
        import toolrecall.replay as R

        from toolrecall.replay import ReplayManager

        m = ReplayManager()
        m.record_call("legacy", "t", {"p": 1}, "old")
        R.start_replay("legacy", scope="r")
        R.start_recording("fresh", scope="w")
        assert R.intercept_call("t", {"p": 1}, scope="r") == {"replay_hit": True, "data": "old"}
        assert R.intercept_call("t", {"p": 2}, scope="w") == {
            "replay_hit": False,
            "message": "recording",
        }
        R.stop(scope="r")
        R.stop(scope="w")

    def test_stop_removes_scope_from_registry(self):
        import toolrecall.replay as R

        R.start_recording("s", scope="gone")
        R.stop(scope="gone")
        assert "gone" not in R._scopes
        # legacy singleton stop unaffected
        R.stop()

    def test_stop_never_touched_legacy_when_scope_stopped(self):
        import toolrecall.replay as R

        R.start_recording("main", scope=None)
        R.start_recording("side", scope="x")
        R.stop(scope="x")
        assert R.status()["replay_mode"] == "record"
        R.stop()

    def test_intercept_inactive_returns_none(self):
        import toolrecall.replay as R

        assert R.intercept_call("t", {}, scope="nothing") is None


# ---- tree-mode interception through scoped API --------------------------------


class TestTreeModeIntercept:
    def _tree(self, m):
        tree = m.fork_tree("tm")
        tid, root = tree["tree_id"], tree["root"]
        m.record_call_at(tid, root, "cached_read", {"path": "/a.txt"}, "SHARED")
        b1 = m.fork_node(tid, root, "b1")
        m.record_call_at(tid, b1["node_id"], "cached_read", {"path": "/a.txt"}, "B1")
        return tid, root, b1["node_id"]

    def test_replay_at_via_intercept(self):
        import toolrecall.replay as R

        from toolrecall.replay import ReplayManager

        m = ReplayManager()
        tid, root, b1 = self._tree(m)
        R.start_replay("tm", scope="x")
        assert R.intercept_call(
            "cached_read", {"path": "/a.txt"}, scope="x", tree_id=tid, node_id=b1
        ) == {
            "replay_hit": True,
            "data": "B1",
        }
        assert R.intercept_call(
            "cached_read", {"path": "/a.txt"}, scope="x", tree_id=tid, node_id=root
        ) == {
            "replay_hit": True,
            "data": "SHARED",
        }
        R.stop(scope="x")

    def test_record_response_tree_path(self):
        import toolrecall.replay as R

        from toolrecall.replay import ReplayManager

        m = ReplayManager()
        tid, root, b1 = self._tree(m)
        R.start_recording("tm", scope="y")
        out = R.record_response(
            "cached_read", {"path": "/z"}, "Z", scope="y", tree_id=tid, node_id=root
        )
        assert out["replay_mode"] == "record" and out["tree"]["call_index"] == 1
        assert m.replay_at(tid, root, "cached_read", {"path": "/z"}) == "Z"
        R.stop(scope="y")
        m.delete_tree(tid)

    def test_miss_message_on_tree_replay(self):
        import toolrecall.replay as R

        from toolrecall.replay import ReplayManager

        m = ReplayManager()
        tid, root, b1 = self._tree(m)
        R.start_replay("tm", scope="x")
        assert R.intercept_call(
            "cached_read", {"path": "/nope"}, scope="x", tree_id=tid, node_id=b1
        ) == {
            "replay_hit": False,
            "message": "No replay found for cached_read",
        }
        R.stop(scope="x")
        m.delete_tree(tid)


# ---- G4: ContextTracker branch isolation --------------------------------------


class TestTrackerBranchScoping:
    def _tracker(self, branch_id=None):
        from toolrecall.context_tracker import ContextTracker

        return ContextTracker(ignore_prefixes=[], branch_id=branch_id)

    def test_branch_scoped_snapshot_restore_roundtrip(self, tmp_path):
        f1 = tmp_path / "a.txt"
        f1.write_text("x")
        t = self._tracker("branch-9")
        t.mark_read(str(f1))
        snap = t.snapshot()
        assert snap["saved"] is True
        # A DIFFERENT tracker instance (simulating restart) restores it
        t2 = self._tracker("branch-9")
        res = t2.restore()
        assert res["restored"] is True and res["reason"] == "ok"
        assert str(f1) in t2._read_set

    def test_branches_do_not_see_each_other(self, tmp_path):
        fa = tmp_path / "a.txt"
        fa.write_text("a")
        fb = tmp_path / "b.txt"
        fb.write_text("b")
        ta = self._tracker("bra")
        ta.mark_read(str(fa))
        assert ta.snapshot()["saved"]
        tb = self._tracker("brb")
        tb.mark_read(str(fb))
        assert tb.snapshot()["saved"]
        # restore bra: only fa, never fb
        ra = self._tracker("bra")
        assert ra.restore()["restored"] is True
        assert str(fa) in ra._read_set
        assert str(fb) not in ra._read_set
        rb = self._tracker("brb")
        assert rb.restore()["restored"] is True
        assert str(fb) in rb._read_set
        assert str(fa) not in rb._read_set

    def test_branch_persistence_never_touches_singleton_row(self, tmp_path):
        f = tmp_path / "a.txt"
        f.write_text("x")
        legacy = self._tracker(None)
        legacy.mark_read(str(f))
        assert legacy.snapshot()["saved"]
        tb = self._tracker("bra")
        tb.mark_read(str(f))
        assert tb.snapshot()["saved"]
        # branch restore pulls branch row, not the singleton
        rb = self._tracker("bra")
        assert rb.restore()["restored"] is True
        # singleton still intact
        r0 = self._tracker(None)
        assert r0.restore()["restored"] is True
        assert str(f) in r0._read_set

    def test_branch_reset_clears_only_own_row(self, tmp_path):
        f = tmp_path / "a.txt"
        f.write_text("x")
        ta = self._tracker("bra")
        ta.mark_read(str(f))
        assert ta.snapshot()["saved"]
        tb = self._tracker("brb")
        tb.mark_read(str(f))
        assert tb.snapshot()["saved"]
        ta.reset()
        # bra's row gone, brb's intact
        ra = self._tracker("bra")
        assert ra.restore()["reason"] == "no-state"
        rb = self._tracker("brb")
        assert rb.restore()["reason"] == "ok"

    def test_legacy_reset_still_clears_singleton(self, tmp_path):
        f = tmp_path / "a.txt"
        f.write_text("x")
        legacy = self._tracker(None)
        legacy.mark_read(str(f))
        assert legacy.snapshot()["saved"]
        legacy.reset()
        r0 = self._tracker(None)
        assert r0.restore()["reason"] == "no-state"
