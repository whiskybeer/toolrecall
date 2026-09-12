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
    """'overlay' | 'snapshot' | 'absent'"""
    if os.path.exists(os.path.join(sp, PTH_NAME)):
        return "overlay"
    if os.path.isdir(os.path.join(sp, "toolrecall")):
        return "snapshot"
    return "absent"


def apply() -> int:
    sp = find_venv_sp()
    if sp is None:
        print("pipx venv for toolrecall not found — run: pipx install /path/to/repo")
        return 1
    if not repo_import_ok():
        print("REFUSING: the repo does not import cleanly (syntax/broken tree).")
        return 1
    # drop the snapshot copy so the .pth wins cleanly
    snap = os.path.join(sp, "toolrecall")
    if os.path.isdir(snap):
        import shutil

        shutil.rmtree(snap)
    pth_path = os.path.join(sp, PTH_NAME)
    with open(pth_path, "w") as f:
        f.write(REPO + "\n")
    print(f"overlay applied: {pth_path} -> {REPO}")
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
