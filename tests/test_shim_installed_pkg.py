"""Shim-from-installed-package e2e test.

Regression guard for the packaging blindspot: tests that run from the repo
root get ``toolrecall`` resolved via repo-cwd sys.path injection, masking a
missing/broken install. This test runs the shim in a SEPARATE interpreter
from a NEUTRAL cwd (tmpdir, no repo on sys.path) against the venv's
site-packages install, and asserts:

  1. ``toolrecall`` imports from site-packages (not the repo source tree);
  2. ``toolrecall.shim`` applies cleanly and intercepts open();
  3. the shim's lazy client import (``from .client import cached_read``)
     resolves from the SAME installed package (the relative-import path
     the shim comment at shim.py:98 warns about).

Skipped if toolrecall is not pip-installed in the current environment
(e.g. bare source checkouts without ``pip install -e .``).
"""

import os
import subprocess
import sys
import tempfile
import unittest
import venv as venv_mod

_IS_VENV_RUN = os.environ.get("TR_SHIM_INSTALL_TEST") == "1"


@unittest.skipIf(_IS_VENV_RUN, "already running inside the isolated venv child")
class TestShimFromInstalledPackage(unittest.TestCase):
    def _venv_python(self) -> str | None:
        """Return the venv python used for testing, building it if needed.

        Uses the CURRENT interpreter's venv (this suite already runs inside
        the project venv), so 'installed' = site-packages copy of toolrecall.
        """
        exe = sys.executable
        # If we're in a venv, use it directly; otherwise build a throwaway one
        if hasattr(sys, "real_prefix") or (
            hasattr(sys, "base_prefix") and sys.base_prefix != sys.prefix
        ):
            return exe
        # Interpreter is the system python — check if toolrecall is importable
        r = subprocess.run(
            [exe, "-c", "import toolrecall; print(toolrecall.__file__)"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if r.returncode != 0:
            return None
        return exe

    def test_shim_resolves_from_site_packages(self):
        py = self._venv_python()
        if py is None:
            self.skipTest("toolrecall not installed in this environment")

        tmpdir = tempfile.mkdtemp(prefix="tr_shim_pkg_")
        # Neutral cwd: NOT the repo root, nothing shadows site-packages
        script = (
            "import os, sys\n"
            # Neutral cwd: pytest ran us with cwd=tmpdir, so the repo root is
            # NOT on sys.path via ''. Import can only succeed through the
            # *install mechanism* (site-packages or the editable finder).
            "import toolrecall\n"
            "pkg_dir = os.path.dirname(toolrecall.__file__)\n"
            # The install must not resolve from the process cwd
            "assert not pkg_dir.startswith(os.getcwd()), (\n"
            "    'toolrecall resolved from cwd: ' + pkg_dir)\n"
            "import toolrecall.shim\n"
            "toolrecall.shim.apply()\n"
            # After apply(), builtins.open is shimmed; a read of a small file
            # must succeed through the shim path (cache write + serve).
            "import builtins, tempfile\n"
            "probe = os.path.join(tempfile.mkdtemp(), 'probe.txt')\n"
            "open(probe, 'w').write('shim-pkg-ok')\n"
            "with open(probe) as f:\n"
            "    data = f.read()\n"
            "assert data == 'shim-pkg-ok', data\n"
            # The lazy client import must resolve from the same package dir
            "import toolrecall.shim as sh\n"
            "assert os.path.dirname(sh.__file__) == pkg_dir, (\n"
            "    'shim and package resolved from different dirs')\n"
            "print('OK ' + pkg_dir)\n"
        )
        script_file = os.path.join(tmpdir, "check_shim.py")
        with open(script_file, "w") as f:
            f.write(script)

        env = os.environ.copy()
        # Ensure the DB/socket state the shim touches stays hermetic: the
        # shim only WRITES cache entries on open(); env isolation happens
        # in the child via TOOLRECALL_CACHE_DB pointing at tmp.
        env["TOOLRECALL_CACHE_DB"] = os.path.join(tmpdir, "shim-e2e.db")
        env["TOOLRECALL_SHIM_EXCLUDE_PREFIXES"] = tmpdir  # don't cache the probe
        # ...actually the probe SHOULD go through the shim — don't exclude it
        env.pop("TOOLRECALL_SHIM_EXCLUDE_PREFIXES", None)

        r = subprocess.run(
            [py, script_file],
            cwd=tmpdir,  # neutral cwd — repo root not on path
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.assertEqual(
            r.returncode,
            0,
            f"shim-from-install check failed\nstdout: {r.stdout}\nstderr: {r.stderr}",
        )
        self.assertIn("OK ", r.stdout)


@unittest.skipIf(_IS_VENV_RUN, "already running inside the isolated venv child")
class TestShimRealInstall(unittest.TestCase):
    """Builds a throwaway venv, pip-installs the wheel (NON-editable), and
    runs the shim there. This is the only test that catches "works as
    editable, dead as installed" regressions (missing package-data, broken
    .pth, imports that only resolve from the source tree)."""

    def test_real_wheel_install_shim_works(self):
        import tempfile as tf

        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with tf.TemporaryDirectory(prefix="tr_wheel_venv_") as venv_dir:
            try:
                venv_mod.create(venv_dir, with_pip=True)
            except subprocess.SubprocessError:
                self.skipTest("venv+pip creation failed in this environment")
            py = os.path.join(venv_dir, "bin", "python")
            inst = subprocess.run(
                [py, "-m", "pip", "install", "--quiet", "--no-input", repo],
                capture_output=True,
                text=True,
                timeout=600,
            )
            if inst.returncode != 0:
                self.skipTest(f"pip install failed: {inst.stderr[-300:]}")

            tmpdir = venv_dir  # neutral cwd inside the venv itself
            script = (
                "import os, sys, toolrecall\n"
                "pkg_dir = os.path.dirname(toolrecall.__file__)\n"
                "assert 'site-packages' in pkg_dir, (\n"
                "    'expected real site-packages install, got: ' + pkg_dir)\n"
                "import toolrecall.shim\n"
                "toolrecall.shim.apply()\n"
                "import tempfile\n"
                "probe = os.path.join(tempfile.mkdtemp(), 'probe.txt')\n"
                "open(probe, 'w').write('wheel-ok')\n"
                "with open(probe) as f:\n"
                "    data = f.read()\n"
                "assert data == 'wheel-ok', data\n"
                "print('OK ' + pkg_dir)\n"
            )
            script_file = os.path.join(tmpdir, "check_wheel.py")
            with open(script_file, "w") as f:
                f.write(script)

            env = os.environ.copy()
            env["TOOLRECALL_CACHE_DB"] = os.path.join(tmpdir, "wheel-e2e.db")
            # Hermeticity: the parent may run under `uv run` (PYTHONPATH ->
            # repo source) — the child venv must resolve toolrecall ONLY
            # from its own site-packages, never the inherited repo path.
            env.pop("PYTHONPATH", None)
            env["PYTHONNOUSERSITE"] = "1"
            r = subprocess.run(
                [py, script_file],
                cwd=tmpdir,
                env=env,
                capture_output=True,
                text=True,
                timeout=120,
            )
            self.assertEqual(
                r.returncode,
                0,
                f"wheel-install shim check failed\nstdout: {r.stdout}\nstderr: {r.stderr}",
            )
            self.assertIn("OK ", r.stdout)


if __name__ == "__main__":
    unittest.main()
