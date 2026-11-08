#!/usr/bin/env python3
"""When does ToolRecall replay pay off? Repeat scenarios on the SWE-bench django tasks.

Per task, both arms run the same sequence (arm B = proxy only, no hook; tight allowlist):
  base r1        cold run (fills arm B's cache)
  base r2        identical rerun                 → best case
  newcommit r1   rerun on base + one unrelated docs commit (CI on the next commit)
  promptedit r1  rerun with one sentence added to the prompt (eval iteration)
  shuffle r1     cold run with `--shuffle` tests (random seed in every test output)
  shuffle r2     rerun of that                   → volatile tool output
Usage: ab_scen.py <proxy_port> [task ...]   (rows appended to $AB_OUT/runs.jsonl)
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # python3 -I drops the script dir
import ab_swe as ab  # noqa: E402

STEPS = [("base", 1), ("base", 2), ("newcommit", 1), ("promptedit", 1), ("shuffle", 1), ("shuffle", 2)]


def done():
    p = ab.OUT / "runs.jsonl"
    if not p.exists():
        return set()
    return {(r["task"], r["arm"], r.get("scenario"), r["rep"]) for r in map(json.loads, p.read_text().splitlines())}


def main():
    port = int(sys.argv[1])
    tasks = sys.argv[2:] or ab.TASKS
    finished = done()
    for k, iid in enumerate(tasks):
        for n, (scenario, rep) in enumerate(STEPS):
            arms = ["A", "B"] if (k + n) % 2 else ["B", "A"]
            for arm in arms:
                if (iid, arm, scenario, rep) in finished:
                    continue
                ab.wait_for_memory()
                ab.run_one(iid, arm, rep, port, scenario=scenario, hook=False, allowed=ab.ALLOWED_TIGHT)


if __name__ == "__main__":
    main()
