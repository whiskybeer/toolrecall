"""Tests for toolrecall.venvs (generic venv discovery + shim helper) and the
agent-agnostic `toolrecall shim` CLI parsing (BUG-1 regressions).

Discovery / ensure_shim tests are deliberately hermetic:
  - `discover_python_venvs` excludes toolrecall's own env and returns entries
    for real venvs on this machine when present.
  - `ensure_shim` against a real temp venv (created via `python -m venv`) proves
    the package-presence -> .pth -> probe chain end to end. The temp venv is
    created fresh, so the .pth is in a scratch location that never touches a
    live agent.
  - The `--venv` parsing tests route around real site-packages by patching
    `_site_packages_of`, so they never write to a real venv.
"""

import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from toolrecall import venvs as venv_mod  # noqa: E402


class TestVenvDiscovery(unittest.TestCase):
    def test_discover_excludes_toolrecall_own_env(self):
        """toolrecall's own running interpreter is never returned."""
        found = venv_mod.discover_python_venvs()
        self.assertIsInstance(found, list)
        own_exec = os.path.realpath(sys.executable)
        for v in found:
            self.assertNotEqual(
                os.path.realpath(v.python),
                own_exec,
                "discovery must exclude toolrecall's own interpreter",
            )

    def test_site_packages_of_venv_like_python_resolves(self):
        """Probing a venv python returns a real site-packages dir.

        The system interpreter (/usr/bin/python3) uses Debian dist-packages and
        is not a venv, so None is acceptable there. We verify against any venv
        interpreter discovery found (which always resolves).
        """
        found = venv_mod.discover_python_venvs()
        # discovery only returns venvs whose site-packages resolved, so any
        # entry proves the probe works end-to-end
        for v in found:
            self.assertTrue(v.site_packages.endswith("site-packages"))
            self.assertTrue(os.path.isdir(v.site_packages))


class TestEnsureShimRealVenv(unittest.TestCase):
    """Run the full ensure_shim chain against a scratch venv.

    The `toolrecall` package is installed editable from the repo so the .pth
    import will actually resolve — proving the probe gate isn't a false pass.
    If the platform lacks `python3 -m venv` / pip, these are skipped rather
    than failing on infrastructure.
    """

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="tr-venvtest-")
        cls.venv_root = os.path.join(cls.tmp, "venv")
        try:
            # --system-site-packages lets the .pth import resolve `toolrecall`
            # from the base interpreter without a (possibly offline) pip step.
            r = subprocess.run(
                [sys.executable, "-m", "venv", "--system-site-packages", cls.venv_root],
                capture_output=True,
                text=True,
                timeout=120,
            )
            if r.returncode != 0:
                raise RuntimeError(f"venv creation failed: {r.stderr[:300]}")
            py = os.path.join(cls.venv_root, "bin", "python")
            sp = venv_mod._site_packages_of(py)
            if not sp:
                raise RuntimeError("could not resolve scratch venv site-packages")
            cls.py = py
            cls.sp = sp
            cls.skip = False
        except Exception as e:  # noqa: BLE001
            cls.skip = True
            cls.skip_reason = str(e)
            cls.py = cls.sp = None

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _v(self):
        return venv_mod.Venv(root=self.venv_root, python=self.py, site_packages=self.sp)

    def test_ensure_shim_happy_path(self):
        if self.skip:
            self.skipTest(self.skip_reason)
        venv = self._v()
        # ensure idempotent double-run
        self.assertTrue(venv_mod.ensure_shim(venv))
        self.assertTrue(venv_mod.ensure_shim(venv))
        self.assertTrue(
            os.path.isfile(os.path.join(self.sp, "tr_shim.pth")),
            ".pth must be present after ensure_shim",
        )
        st = venv_mod.shim_status(venv)
        self.assertTrue(st["package_importable"])
        self.assertTrue(st["pth_present"])
        self.assertTrue(st["probe_ok"])

    def test_uninstall_shim_removes_pth(self):
        if self.skip:
            self.skipTest(self.skip_reason)
        venv = self._v()
        self.assertTrue(venv_mod.ensure_shim(venv))
        self.assertTrue(venv_mod.uninstall_shim(venv))
        self.assertFalse(
            os.path.exists(os.path.join(self.sp, "tr_shim.pth")),
            ".pth must be gone after uninstall",
        )


class TestShimCLIArgParsing(unittest.TestCase):
    """BUG-1 regressions: `cmd_shim` must honor --venv / --all, unknown flags fail.

    The `_site_packages_of` subprocess is patched to a fake dir so nothing
    writes to a real Python env. We only exercise parsing + dispatch decisions.
    """

    def run_cmd(self, argv):
        old_argv, old_stdout = sys.argv, sys.stdout
        buf = io.StringIO()
        sys.stdout = buf
        sys.argv = ["toolrecall", "shim"] + argv
        try:
            from toolrecall import cli

            try:
                cli.cmd_shim()
            except SystemExit as e:
                buf.write(f"[exit {e.code}]")
            return buf.getvalue()
        finally:
            sys.argv, sys.stdout = old_argv, old_stdout

    @patch.object(venv_mod, "_site_packages_of", return_value="/tmp/fake-sp")
    def test_unknown_flag_fails(self, _mock):
        out = self.run_cmd(["--install", "--bogus"])
        self.assertIn("Error: unknown shim flag", out)

    def test_venv_missing_arg_fails(self):
        out = self.run_cmd(["--install", "--venv"])
        self.assertIn("--venv requires a path", out)

    @patch.object(venv_mod, "_site_packages_of", return_value="/tmp/fake-sp")
    @patch("builtins.input", return_value="n")
    def test_install_venv_opt_in_default_no(self, _input, _mock):
        """--venv targets that venv and default opt-in is NO (no .pth written)."""
        tmp = tempfile.mkdtemp()
        venv_root = os.path.join(tmp, "fake")
        bin_dir = os.path.join(venv_root, "bin")
        os.makedirs(bin_dir)
        py = os.path.join(bin_dir, "python3")
        with open(py, "w") as f:
            f.write("#!/bin/sh\n")
        os.chmod(py, 0o755)
        cfg = os.path.join(venv_root, "pyvenv.cfg")
        with open(cfg, "w") as f:
            f.write("[venv]\n")
        try:
            out = self.run_cmd(["--install", "--venv", venv_root])
            self.assertIn("skipped (opt-out)", out)
            # no .pth written anywhere real
            self.assertFalse(os.path.exists("/tmp/fake-sp/tr_shim.pth"))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestConfirmInstall(unittest.TestCase):
    """W4: prompt defaults to disabled; explicit yes enables."""

    def _fake_venv(self):
        return venv_mod.Venv(root="/fake/root", python="/fake/python", site_packages="/fake/sp")

    def test_eof_returns_false(self):
        stdout = io.StringIO()
        old_stdout = sys.stdout
        sys.stdout = stdout
        try:
            with patch("builtins.input", side_effect=EOFError):
                from toolrecall.cli import _confirm_install

                self.assertFalse(_confirm_install(self._fake_venv()))
        finally:
            sys.stdout = old_stdout

    def test_yes_returns_true(self):
        from toolrecall.cli import _confirm_install

        with patch("builtins.input", side_effect=["y"]):
            self.assertTrue(_confirm_install(self._fake_venv()))

    def test_no_returns_false(self):
        from toolrecall.cli import _confirm_install

        with patch("builtins.input", side_effect=["n"]):
            self.assertFalse(_confirm_install(self._fake_venv()))


class TestShimManifestDrift(unittest.TestCase):
    """Content-based shim identity — the drift a version string cannot see.

    Regression: the shim copy inside an agent venv was a hand-synced snapshot
    whose dist-info claimed an older version while its code was 60 commits
    behind. `ensure_shim` compared versions, pip answered "requirement already
    satisfied", and `shim --status` printed ✅ — the venv kept pre-fix shim
    behaviour indefinitely. Identity is content, so content is what is checked.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="tr-manifest-test-")
        self.a = os.path.join(self.tmp, "a")
        self.b = os.path.join(self.tmp, "b")
        for d in (self.a, self.b):
            os.makedirs(d)
        for name in venv_mod._SHIM_MANIFEST_FILES:
            for d in (self.a, self.b):
                with open(os.path.join(d, name), "w") as fh:
                    fh.write(f"# {name}\n")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _fake_venv(self):
        return venv_mod.Venv(
            root=os.path.join(self.tmp, "venv"),
            python=sys.executable,
            site_packages=os.path.join(self.tmp, "sp"),
        )

    def test_identical_trees_fingerprint_equal(self):
        self.assertEqual(venv_mod._manifest_of(self.a), venv_mod._manifest_of(self.b))

    def test_one_byte_change_changes_fingerprint(self):
        with open(os.path.join(self.b, "shim.py"), "a") as fh:
            fh.write("# drift\n")
        self.assertNotEqual(venv_mod._manifest_of(self.a), venv_mod._manifest_of(self.b))

    def test_half_removed_copy_is_not_a_match(self):
        os.remove(os.path.join(self.b, "stub_core.py"))
        self.assertNotEqual(venv_mod._manifest_of(self.a), venv_mod._manifest_of(self.b))

    def test_empty_dir_has_no_manifest(self):
        empty = os.path.join(self.tmp, "empty")
        os.makedirs(empty)
        self.assertIsNone(venv_mod._manifest_of(empty))

    def test_status_flags_drift_when_versions_agree(self):
        with (
            patch.object(venv_mod, "_running_manifest", return_value="aaa"),
            patch.object(venv_mod, "_venv_manifest", return_value="bbb"),
            patch.object(venv_mod, "_running_version", return_value="0.8.22"),
            patch.object(venv_mod, "_installed_version", return_value="0.8.22"),
            patch.object(venv_mod, "_importable", return_value=True),
        ):
            st = venv_mod.shim_status(self._fake_venv())
        self.assertTrue(st["drift"])
        self.assertEqual(st["version_venv"], st["version_source"])

    def test_status_in_sync_when_content_matches_despite_version(self):
        with (
            patch.object(venv_mod, "_running_manifest", return_value="aaa"),
            patch.object(venv_mod, "_venv_manifest", return_value="aaa"),
            patch.object(venv_mod, "_running_version", return_value="0.8.22"),
            patch.object(venv_mod, "_installed_version", return_value="0.8.21"),
            patch.object(venv_mod, "_importable", return_value=True),
        ):
            st = venv_mod.shim_status(self._fake_venv())
        self.assertFalse(st["drift"])

    def test_ensure_shim_fails_when_drift_persists(self):
        """No false "verified": a still-behind venv reports failure."""
        with (
            patch.object(venv_mod, "_running_manifest", return_value="aaa"),
            patch.object(venv_mod, "_venv_manifest", return_value="bbb"),
            patch.object(venv_mod, "_install_toolrecall", return_value=True),
            patch.object(venv_mod, "_source_root", return_value=None),
            patch.object(venv_mod, "_importable", return_value=True),
        ):
            self.assertFalse(venv_mod.ensure_shim(self._fake_venv()))

    def test_ensure_shim_converges_source_tree_via_overlay(self):
        """A source-tree install pulls a drifted venv onto the live tree."""
        venv = self._fake_venv()
        os.makedirs(venv.site_packages, exist_ok=True)
        stale = os.path.join(venv.site_packages, "toolrecall")
        os.makedirs(stale)
        with open(os.path.join(stale, "shim.py"), "w") as fh:
            fh.write("# stale\n")
        state = {"drifted": True}
        real_overlay = venv_mod._overlay_into  # capture before patching

        def fake_manifest(_python):
            return "bbb" if state["drifted"] else "aaa"

        def fake_overlay(v, root):
            state["drifted"] = False  # overlay lands → content now matches
            return real_overlay(v, root)

        with (
            patch.object(venv_mod, "_running_manifest", return_value="aaa"),
            patch.object(venv_mod, "_venv_manifest", side_effect=fake_manifest),
            patch.object(venv_mod, "_install_toolrecall", return_value=True),
            patch.object(venv_mod, "_source_root", return_value=self.tmp),
            patch.object(venv_mod, "_overlay_into", side_effect=fake_overlay),
            patch.object(venv_mod, "_pth_source", return_value=os.path.join(self.a, "shim.py")),
            patch.object(venv_mod, "_importable", return_value=True),
            patch.object(venv_mod, "_shim_applied", return_value=True),
        ):
            self.assertTrue(venv_mod.ensure_shim(venv))
        self.assertTrue(
            os.path.isfile(os.path.join(venv.site_packages, venv_mod._OVERLAY_PTH)),
            "overlay .pth must be written on drift",
        )
        self.assertFalse(os.path.isdir(stale), "shadowing snapshot dir must be removed")

    def test_uninstall_removes_overlay_pth(self):
        venv = self._fake_venv()
        os.makedirs(venv.site_packages, exist_ok=True)
        p = os.path.join(venv.site_packages, venv_mod._OVERLAY_PTH)
        with open(p, "w") as fh:
            fh.write("/somewhere\n")
        self.assertTrue(venv_mod.uninstall_shim(venv))
        self.assertFalse(os.path.exists(p))


if __name__ == "__main__":
    unittest.main()
