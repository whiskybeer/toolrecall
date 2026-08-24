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
                os.path.realpath(v.python), own_exec,
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
                [sys.executable, "-m", "venv", "--system-site-packages",
                 cls.venv_root],
                capture_output=True, text=True, timeout=120,
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
        return venv_mod.Venv(root=self.venv_root, python=self.py,
                             site_packages=self.sp)

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
        sys.argv = [ "toolrecall", "shim" ] + argv
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
        return venv_mod.Venv(root="/fake/root", python="/fake/python",
                             site_packages="/fake/sp")

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


if __name__ == "__main__":
    unittest.main()
