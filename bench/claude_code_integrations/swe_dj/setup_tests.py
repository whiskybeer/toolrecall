"""setup_tests.py — convert SWE-bench Lite django test ids into runtests.py labels.

"test_x (a.b.C)" / "test_x (a.b.C.test_x)" -> "a.b.C.test_x". Docstring ids
(e.g. "#23938 - Changing a ManyToManyField into a concrete field") are resolved
by finding the docstring in the test sources at base_commit + test_patch and
taking the enclosing test method and class. Writes inst/<ID>/labels.json.
"""

import json
import os
import re
import subprocess
import sys
import tempfile

D = os.path.dirname(os.path.abspath(__file__))
ID_RE = re.compile(r"^(\w+) \(([\w.]+)\)$")


def resolve_docstring(tree, text):
    first = text.strip().splitlines()[0][:60]
    hits = subprocess.run(["grep", "-rlF", first, "tests/"], cwd=tree, capture_output=True, text=True).stdout.split()
    for rel in hits:
        src = open(os.path.join(tree, rel)).read()
        pos = src.find(first)
        before = src[:pos]
        m_def = list(re.finditer(r"^\s+def (test\w+)\(", before, re.M))
        m_cls = list(re.finditer(r"^class (\w+)\(", before, re.M))
        if m_def and m_cls:
            mod = rel[len("tests/"):-3].replace("/", ".")
            return f"{mod}.{m_cls[-1].group(1)}.{m_def[-1].group(1)}"
    return None


def labels_for(iid):
    inst = f"{D}/inst/{iid}"
    t = json.load(open(f"{inst}/tests.json"))
    base = open(f"{inst}/base_commit").read().strip()
    tree = tempfile.mkdtemp(prefix="tmp-lbl.", dir=D)
    subprocess.run(["git", "-C", f"{D}/django", "worktree", "add", "-f", "--detach", tree, base],
                   capture_output=True, check=True)
    try:
        subprocess.run(["patch", "-p1", "-s", "-i", f"{inst}/test_patch"], cwd=tree, check=True)
        out, unresolved = {}, []
        for key in ("f2p", "p2p"):
            labels = []
            for x in t[key]:
                m = ID_RE.match(x)
                if m:
                    name, path = m.groups()
                    labels.append(path if path.endswith("." + name) else f"{path}.{name}")
                else:
                    lab = resolve_docstring(tree, x)
                    (labels.append(lab) if lab else unresolved.append((key, x)))
            out[key] = list(dict.fromkeys(labels))
        json.dump(out, open(f"{inst}/labels.json", "w"), indent=1)
        return out, unresolved
    finally:
        subprocess.run(["git", "-C", f"{D}/django", "worktree", "remove", "--force", tree], capture_output=True)


for iid in sys.argv[1:] or sorted(os.listdir(f"{D}/inst")):
    out, unresolved = labels_for(iid)
    print(iid, "f2p", len(out["f2p"]), "p2p", len(out["p2p"]), "unresolved", unresolved[:3], len(unresolved))
