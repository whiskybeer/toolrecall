"""RED-first tests for scripts/repo_overlay.py state detection.

The post-commit hook trusts ``--check``:

    OUT=$(python3 scripts/repo_overlay.py --check)
    if echo "$OUT" | grep -q "state: overlay"; then exit 0; fi

``overlay_state()`` returned ``"overlay"`` whenever the ``.pth`` existed — even
with the installed snapshot directory still present. The snapshot precedes the
``.pth`` on ``sys.path``, so it wins every import: the hook exited early forever
and CLI/cron kept importing the frozen package while ``--check`` claimed the
repo tree was live.
"""

import contextlib
import importlib.util
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "repo_overlay.py"
PTH = "zz_toolrecall_repo.pth"


def _load():
    spec = importlib.util.spec_from_file_location("repo_overlay_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Base(unittest.TestCase):
    def setUp(self):
        self.mod = _load()
        self.sp = tempfile.mkdtemp(prefix="tr_overlay_")
        self.addCleanup(lambda: __import__("shutil").rmtree(self.sp, ignore_errors=True))

    def _pth(self):
        with open(os.path.join(self.sp, PTH), "w") as f:
            f.write("/repo\n")

    def _snap(self):
        os.makedirs(os.path.join(self.sp, "toolrecall"), exist_ok=True)
        with open(os.path.join(self.sp, "toolrecall", "__init__.py"), "w") as f:
            f.write("")


class OverlayStateTest(_Base):
    def test_absent_when_neither_is_present(self):
        self.assertEqual(self.mod.overlay_state(self.sp), "absent")

    def test_snapshot_only(self):
        self._snap()
        self.assertEqual(self.mod.overlay_state(self.sp), "snapshot")

    def test_overlay_only(self):
        self._pth()
        self.assertEqual(self.mod.overlay_state(self.sp), "overlay")

    def test_pth_plus_snapshot_is_shadowed_not_overlay(self):
        # The defect: the hook saw "overlay" and never re-applied.
        self._pth()
        self._snap()
        self.assertEqual(self.mod.overlay_state(self.sp), "shadowed")


class ApplyTest(_Base):
    def test_apply_clears_the_snapshot_and_writes_the_pth(self):
        self._pth()
        self._snap()
        self.assertEqual(self.mod.apply_to(self.sp), 0)
        self.assertEqual(self.mod.overlay_state(self.sp), "overlay")
        self.assertFalse(os.path.isdir(os.path.join(self.sp, "toolrecall")))
        with open(os.path.join(self.sp, PTH)) as f:
            self.assertEqual(f.read().strip(), self.mod.REPO)

    def test_apply_is_idempotent(self):
        self._pth()
        self._snap()
        self.mod.apply_to(self.sp)
        self.assertEqual(self.mod.apply_to(self.sp), 0)
        self.assertEqual(self.mod.overlay_state(self.sp), "overlay")


class CheckContractTest(_Base):
    """The hook greps for exactly 'state: overlay' — anything else must move it."""

    def _check(self) -> tuple[int, str]:
        buf = io.StringIO()
        saved_argv = sys.argv
        saved_find = self.mod.find_venv_sp
        sys.argv = ["repo_overlay.py", "--check"]
        self.mod.find_venv_sp = lambda: self.sp
        try:
            with contextlib.redirect_stdout(buf):
                rc = self.mod.main()
        finally:
            sys.argv = saved_argv
            self.mod.find_venv_sp = saved_find
        return rc, buf.getvalue()

    def test_check_on_a_shadowed_tree_does_not_say_overlay(self):
        self._pth()
        self._snap()
        rc, out = self._check()
        self.assertEqual(rc, 0)
        self.assertIn("state: shadowed", out)
        self.assertNotIn("state: overlay", out)

    def test_check_on_a_clean_overlay_says_overlay(self):
        self._pth()
        rc, out = self._check()
        self.assertEqual(rc, 0)
        self.assertIn("state: overlay", out)


class ApplyGuardTest(_Base):
    """The overlay must never deploy unmerged work to the live venv."""

    def _apply(self, branch: str, argv_extra: list[str] | None = None) -> tuple[int, str]:
        buf = io.StringIO()
        saved_argv = sys.argv
        saved = (
            self.mod.find_venv_sp,
            self.mod.repo_import_ok,
            self.mod.import_source,
        )
        self.mod.find_venv_sp = lambda: self.sp
        self.mod.repo_import_ok = lambda: True
        self.mod.import_source = lambda sp: ""
        self.mod.current_branch = lambda: branch
        sys.argv = ["repo_overlay.py", *(argv_extra or [])]
        try:
            with contextlib.redirect_stdout(buf):
                rc = self.mod.apply()
        finally:
            sys.argv = saved_argv
            (
                self.mod.find_venv_sp,
                self.mod.repo_import_ok,
                self.mod.import_source,
            ) = saved
        return rc, buf.getvalue()

    def test_refuses_to_deploy_a_feature_branch(self):
        self._snap()
        rc, out = self._apply("fix/something")
        self.assertEqual(rc, 1)
        self.assertIn("REFUSING", out)
        self.assertTrue(os.path.isdir(os.path.join(self.sp, "toolrecall")))
        self.assertFalse(os.path.exists(os.path.join(self.sp, PTH)))

    def test_applies_on_main(self):
        self._snap()
        rc, out = self._apply("main")
        self.assertEqual(rc, 0)
        self.assertEqual(self.mod.overlay_state(self.sp), "overlay")

    def test_force_bypasses_the_guard(self):
        self._snap()
        rc, _ = self._apply("fix/something", ["--force"])
        self.assertEqual(rc, 0)
        self.assertEqual(self.mod.overlay_state(self.sp), "overlay")


if __name__ == "__main__":
    unittest.main()
