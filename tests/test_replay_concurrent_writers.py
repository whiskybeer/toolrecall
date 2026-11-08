"""Separate processes recording into one replay store must not lose records.

call_index used to be computed with a SELECT MAX(...) + 1 before the INSERT;
the lock around it is an in-process RLock, so two processes on the same DB
picked the same index and one INSERT failed with IntegrityError (6-20% of
records lost with 8 writers, bench/results/FLEET_RESULTS.md Phase 0).
"""

import json
import os
import subprocess
import sys

import pytest

WRITERS = 6
PER_WRITER = 60

_CHILD = r"""
import json, os, sys
from toolrecall.replay import ReplayManager
kind, target, w, k = sys.argv[1], json.loads(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4])
m = ReplayManager()
errors = 0
for j in range(k):
    try:
        if kind == "tree":
            m.record_call_at(target[0], target[1], "read", {"p": f"{w}_{j}"}, f"r{w}_{j}")
        else:
            m.record_call(target, "read", {"p": f"{w}_{j}"}, f"r{w}_{j}")
    except Exception as e:
        errors += 1
        print(type(e).__name__, e, file=sys.stderr)
print(errors)
"""


@pytest.fixture(autouse=True)
def _reset_db_singleton(_isolated_toolrecall_env):
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


def _run_writers(kind: str, target) -> int:
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env = {**os.environ, "PYTHONPATH": repo}
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", _CHILD, kind, json.dumps(target), str(w), str(PER_WRITER)],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for w in range(WRITERS)
    ]
    errors = 0
    for p in procs:
        out, err = p.communicate(timeout=120)
        assert p.returncode == 0, err
        errors += int(out.strip().splitlines()[-1])
    return errors


def _check_all_replay(m, lookup) -> None:
    for w in range(WRITERS):
        for j in range(PER_WRITER):
            assert lookup("read", {"p": f"{w}_{j}"}) == f"r{w}_{j}"


def test_tree_writers_in_separate_processes():
    from toolrecall.replay import ReplayManager

    m = ReplayManager()
    tree = m.fork_tree("concurrent")
    root = tree["root"]
    assert _run_writers("tree", [tree["tree_id"], root]) == 0
    assert m.node_call_count(tree["tree_id"], root) == WRITERS * PER_WRITER
    _check_all_replay(m, lambda t, a: m.replay_at(tree["tree_id"], root, t, a))


def test_flat_writers_in_separate_processes():
    from toolrecall.replay import ReplayManager

    m = ReplayManager()
    assert _run_writers("flat", "concurrent-flat") == 0
    calls = m.get_scenario("concurrent-flat")
    assert sorted(c["call_index"] for c in calls) == list(range(WRITERS * PER_WRITER))
    _check_all_replay(m, lambda t, a: m.find_replay("concurrent-flat", t, a))
