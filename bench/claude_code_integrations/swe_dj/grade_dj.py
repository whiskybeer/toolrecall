"""grade_dj.py ID WORKDIR [--record-gold] → prints "f2p=<0|1> p2p=<0|1>".

Django grading on SWE-bench Lite: apply the official test_patch to a copy,
run the FAIL_TO_PASS labels (must all pass), then run every test module the
PASS_TO_PASS set lives in and compare failing tests against the gold patch's
failures in the same env (SWE-bench ids that are docstrings can't be run by
label, so whole modules stand in for them). --record-gold stores that set.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

D = os.path.dirname(os.path.abspath(__file__))
PY = f"{D}/env/bin/python"
BAD = re.compile(r"^(?:FAIL|ERROR): (\S+) \(([\w.]+)\)", re.M)


def run(tree, labels):
    p = subprocess.run(
        [PY, "runtests.py", "--parallel", "1", "--noinput", *labels],
        cwd=f"{tree}/tests",
        env={**os.environ, "PYTHONPATH": tree},
        capture_output=True,
        text=True,
        timeout=1800,
    )
    out = p.stdout + p.stderr
    return p.returncode, {f"{c}.{n}" if not c.endswith("." + n) else c for n, c in BAD.findall(out)}, out


def main():
    iid, src = sys.argv[1], sys.argv[2]
    inst = f"{D}/inst/{iid}"
    t = json.load(open(f"{inst}/labels.json"))
    tree = tempfile.mkdtemp(prefix="tmp-gdj.", dir=D)
    try:
        shutil.copytree(src, tree, dirs_exist_ok=True, ignore=shutil.ignore_patterns(".git"))
        ap = subprocess.run(["patch", "-p1", "-s", "-i", f"{inst}/test_patch"], cwd=tree)
        if ap.returncode:
            print("f2p=apply-fail p2p=apply-fail")
            return
        rc, _, _ = run(tree, t["f2p"])
        modules = sorted({".".join(x.split(".")[:2]) for x in t["p2p"] + t["f2p"]})
        _, failed, out = run(tree, modules)
        failed -= set(t["f2p"])
        gold_path = f"{inst}/p2p_gold_fail.json"
        if "--record-gold" in sys.argv:
            json.dump(sorted(failed), open(gold_path, "w"))
            print(f"recorded {len(failed)} gold failures in {modules}")
        gold = set(json.load(open(gold_path))) if os.path.exists(gold_path) else set()
        new = failed - gold
        if new:
            open(f"{inst}/last_new_failures.txt", "w").write("\n".join(sorted(new)))
        print(f"f2p={0 if rc == 0 else 1} p2p={0 if not new else 1}")
    finally:
        shutil.rmtree(tree, ignore_errors=True)


main()
