"""Keep the pipx-installed toolrecall in sync with this repo (dev overlay).

Writing `zz_toolrecall_repo.pth` into the pipx venv's site-packages makes
every `toolrecall` invocation import the LIVE repo tree instead of a
frozen snapshot — pipx staleness (3 incidents: TUI, trend, Ctx) becomes
impossible. Safe because the repo is import-validated before the overlay
is written, and reversible by deleting the .pth + `pipx install --force`.

Usage:
  python3 scripts/repo_overlay.py          # apply overlay
  python3 scripts/repo_overlay.py --check  # report status only
"""

import os
import shutil
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PTH_NAME = "zz_toolrecall_repo.pth"


def find_venv_sp() -> str | None:
    """Locate the pipx venv site-packages for toolrecall (if installed)."""
    base = os.path.expanduser("~/.local/pipx/venvs/toolrecall/lib")
    if not os.path.isdir(base):
        return None
    for pydir in sorted(os.listdir(base)):
        sp = os.path.join(base, pydir, "site-packages")
        if os.path.isdir(sp):
            return sp
    return None


def repo_import_ok() -> bool:
    """Validate the repo imports cleanly before pointing anything at it."""
    r = subprocess.run(
        [sys.executable, "-c", f"import sys; sys.path.insert(0, {REPO!r}); import toolrecall"],
        capture_output=True,
        timeout=30,
    )
    return r.returncode == 0


def overlay_state(sp: str) -> str:
    """'overlay' | 'shadowed' | 'snapshot' | 'absent'

    'shadowed' is the case the post-commit hook used to miss: the .pth is
    present, so ``--check`` said "overlay", while the installed snapshot
    directory still precedes it on sys.path and therefore wins every import.
    Presence of the .pth alone is NOT proof the repo tree is live.
    """
    pth = os.path.exists(os.path.join(sp, PTH_NAME))
    snap = os.path.isdir(os.path.join(sp, "toolrecall"))
    if pth and snap:
        return "shadowed"
    if pth:
        return "overlay"
    if snap:
        return "snapshot"
    return "absent"


def apply_to(sp: str) -> int:
    """Point site-packages at the repo tree: drop the snapshot, write the .pth."""
    snap = os.path.join(sp, "toolrecall")
    if os.path.isdir(snap):
        shutil.rmtree(snap)
    with open(os.path.join(sp, PTH_NAME), "w") as f:
        f.write(REPO + "\n")
    return 0


def import_source(sp: str) -> str:
    """Which copy the venv actually imports ('' when it cannot be probed).

    Fail-open: a probe failure is reported as "unknown", never as success. The
    probe is what turns `--check` from a file-existence claim into a statement
    about the running interpreter.
    """
    py = os.path.abspath(os.path.join(sp, "..", "..", "..", "bin", "python"))
    if not os.access(py, os.X_OK):
        return ""
    try:
        r = subprocess.run(
            [py, "-c", "import toolrecall; print(toolrecall.__file__)"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if r.returncode != 0:
        return ""
    return (r.stdout or "").strip()


def current_branch() -> str:
    """Branch checked out in REPO ('' when git cannot answer)."""
    try:
        r = subprocess.run(
            ["git", "-C", REPO, "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return (r.stdout or "").strip() if r.returncode == 0 else ""


def apply() -> int:
    sp = find_venv_sp()
    if sp is None:
        print("pipx venv for toolrecall not found — run: pipx install /path/to/repo")
        return 1
    if not repo_import_ok():
        print("REFUSING: the repo does not import cleanly (syntax/broken tree).")
        return 1
    # The post-commit hook runs this after EVERY commit, on whatever branch is
    # checked out. Deploying an unmerged branch into the live venv is how a
    # half-finished fix reaches the running daemon, so require main explicitly.
    branch = current_branch()
    if branch and branch != "main" and "--force" not in sys.argv:
        print(f"REFUSING: {branch} is checked out — the overlay would deploy unmerged work.")
        print(f"         merge to main first, or run: {sys.argv[0]} --force")
        return 1
    apply_to(sp)
    pth_path = os.path.join(sp, PTH_NAME)
    src = import_source(sp)
    real_repo = os.path.realpath(REPO) + os.sep
    if src and not os.path.realpath(src).startswith(real_repo):
        print(f"WARNING: overlay written but the venv still imports {src}")
        print(f"         expected a path under {REPO} — check sys.path ordering")
        return 1
    suffix = f" (imports {src})" if src else " (import not probed)"
    print(f"overlay applied: {pth_path} -> {REPO}{suffix}")
    return 0


def main() -> int:
    sp = find_venv_sp()
    state = overlay_state(sp) if sp else "no-venv"
    if "--check" in sys.argv:
        print(f"state: {state} | repo: {REPO}")
        return 0
    if state == "overlay":
        print("overlay already active")
        return 0
    return apply()


if __name__ == "__main__":
    sys.exit(main())
