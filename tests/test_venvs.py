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
        """toolrecall's own environment is never returned.

        Identity is the venv root, not the interpreter realpath: other
        environments legitimately share that binary (see TestVenvIdentity).
        """
        found = venv_mod.discover_python_venvs()
        self.assertIsInstance(found, list)
        own_root = os.path.realpath(sys.prefix)
        for v in found:
            self.assertNotEqual(
                os.path.realpath(v.root),
                own_root,
                "discovery must exclude toolrecall's own environment",
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

    @patch.object(venv_mod, "discover_python_venvs")
    def test_install_all_refuses_without_yes(self, mock_discover):
        """--all is machine-wide: non-interactive stdin must refuse, not skip."""
        mock_discover.return_value = [
            venv_mod.Venv(
                root="/tmp/fake-venv",
                python="/tmp/fake-venv/bin/python3",
                site_packages="/tmp/fake-sp",
            )
        ]
        out = self.run_cmd(["--install", "--all"])
        self.assertIn("MACHINE-WIDE SHIM OPERATION", out)
        self.assertIn("Refusing machine-wide install without --yes", out)
        self.assertIn("[exit 2]", out)

    def test_reconcile_without_config_is_noop(self):
        import types

        with patch(
            "toolrecall.config.load_config",
            return_value=types.SimpleNamespace(shim_required_venvs=[]),
        ):
            out = self.run_cmd(["--reconcile"])
        self.assertIn("No [shim].required_venvs configured", out)

    def test_reconcile_dry_run_reports_unhealthy(self):
        import types

        fake = venv_mod.Venv(
            root="/tmp/fake-venv",
            python="/tmp/fake-venv/bin/python3",
            site_packages="/tmp/fake-sp",
        )
        st = {"applied": False, "probe_ok": False, "drift": False}
        with (
            patch(
                "toolrecall.config.load_config",
                return_value=types.SimpleNamespace(shim_required_venvs=["/tmp/*"]),
            ),
            patch.object(venv_mod, "required_venv_targets", return_value=[fake]),
            patch.object(venv_mod, "shim_status", return_value=st),
        ):
            out = self.run_cmd(["--reconcile"])
        self.assertIn("would install", out)
        self.assertIn("[exit 1]", out)


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

    def test_manifest_ignores_a_patched_builtins_open(self):
        """The fingerprint describes disk bytes, not what a cache hook serves.

        ``open()`` is the hook point for the transparent shim (and any similar
        cache). A fingerprint that goes through it can hash stale content and
        report DRIFT for an in-sync venv — or hide real drift behind a hit.
        """
        expected = venv_mod._manifest_of(self.a)
        with patch("builtins.open", side_effect=AssertionError("used builtins.open")):
            got = venv_mod._manifest_of(self.a)
        self.assertEqual(got, expected)

    def test_running_version_prefers_the_imported_code(self):
        """A repo overlay makes dist-info stale; report the code that runs."""
        import types

        fake = types.SimpleNamespace(__version__="0.8.24")
        with (
            patch.dict("sys.modules", {"toolrecall": fake}),
            patch.object(venv_mod, "_metadata_version", return_value="0.8.22"),
        ):
            self.assertEqual(venv_mod._running_version(), "0.8.24")

    def test_status_exposes_metadata_separately(self):
        with (
            patch.object(venv_mod, "_running_manifest", return_value="aaa"),
            patch.object(venv_mod, "_venv_manifest", return_value="aaa"),
            patch.object(venv_mod, "_running_version", return_value="0.8.24"),
            patch.object(venv_mod, "_metadata_version", return_value="0.8.22"),
            patch.object(venv_mod, "_installed_version", return_value="0.8.24"),
            patch.object(venv_mod, "_importable", return_value=True),
        ):
            st = venv_mod.shim_status(self._fake_venv())
        self.assertEqual(st["version_source"], "0.8.24")
        self.assertEqual(st["version_metadata"], "0.8.22")

    def test_absent_package_is_not_drift(self):
        """No copy at all is 'not installed', not 'pre-fix behaviour'."""
        with (
            patch.object(venv_mod, "_running_manifest", return_value="aaa"),
            patch.object(venv_mod, "_venv_manifest", return_value=None),
            patch.object(venv_mod, "_importable", return_value=False),
        ):
            st = venv_mod.shim_status(self._fake_venv())
        self.assertFalse(st["drift"])
        self.assertIsNone(st["manifest_venv"])

    def test_venv_manifest_ignores_a_poisoned_child_hook(self):
        """A hook active in the target interpreter must not change the digest.

        Simulates any file-cache shim: a ``sitecustomize`` that makes every read
        return "poisoned". The target interpreter is still the correct oracle
        for *which copy* it imports; only the bytes must come from disk.
        """
        poison = os.path.join(self.tmp, "poison")
        os.makedirs(poison, exist_ok=True)
        with open(os.path.join(poison, "sitecustomize.py"), "w") as fh:
            fh.write(
                "import builtins, io\n"
                "_real = builtins.open\n"
                "def _p(file, mode='r', *a, **k):\n"
                "    if 'r' in mode and 'b' in mode:\n"
                "        return io.BytesIO(b'poisoned')\n"
                "    if 'r' in mode and '+' not in mode:\n"
                "        return io.StringIO('poisoned')\n"
                "    return _real(file, mode, *a, **k)\n"
                "builtins.open = _p\n"
            )
        import toolrecall

        expected = venv_mod._manifest_of(os.path.dirname(toolrecall.__file__))
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(venv_mod.__file__)))
        with patch.dict(os.environ, {"PYTHONPATH": poison + os.pathsep + repo_root}):
            got = venv_mod._venv_manifest(sys.executable)
        self.assertEqual(got, expected)


class TestVenvIdentity(unittest.TestCase):
    """Two venvs may share one base interpreter and still be distinct targets.

    Every uv/PM venv's ``bin/python3`` is a symlink to a shared CPython, so
    keying environments on the interpreter's realpath collapses them: one is
    probed, the other's site-packages (and `.pth`) are never seen.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="tr-identity-")
        self.base = os.path.join(self.tmp, "base")
        os.makedirs(os.path.join(self.base, "bin"))
        with open(os.path.join(self.base, "pyvenv.cfg"), "w") as fh:
            fh.write("home = /usr/bin\n")
        with open(os.path.join(self.base, "bin", "python3"), "w") as fh:
            fh.write("#!/bin/sh\n")
        os.chmod(os.path.join(self.base, "bin", "python3"), 0o755)
        for name in ("a", "b"):
            root = os.path.join(self.tmp, name)
            os.makedirs(os.path.join(root, "bin"))
            os.makedirs(os.path.join(root, "lib", "python3.14", "site-packages"))
            with open(os.path.join(root, "pyvenv.cfg"), "w") as fh:
                fh.write("home = ../base/bin\n")
            os.symlink(
                os.path.join(self.base, "bin", "python3"),
                os.path.join(root, "bin", "python3"),
            )

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    @unittest.skipIf(os.name == "nt", "symlink layout")
    def test_shared_base_venvs_are_distinct(self):
        a = os.path.join(self.tmp, "a", "bin", "python3")
        b = os.path.join(self.tmp, "b", "bin", "python3")
        self.assertEqual(venv_mod._real(a), venv_mod._real(b))

        def fake_sp(py):
            return os.path.join(
                os.path.dirname(os.path.dirname(py)),
                "lib",
                "python3.14",
                "site-packages",
            )

        with (
            patch.object(venv_mod, "_python_from_running_procs", return_value=[a, b]),
            patch.object(venv_mod, "_candidate_pythons_from_home", return_value=[]),
            patch.object(venv_mod, "_site_packages_of", side_effect=fake_sp),
        ):
            found = venv_mod.discover_python_venvs()
        self.assertEqual(
            sorted(v.root for v in found),
            sorted([venv_mod._real(os.path.join(self.tmp, n)) for n in ("a", "b")]),
        )
        self.assertEqual(len({v.site_packages for v in found}), 2)

    def test_venv_root_of_plain_interpreter_is_none(self):
        plain = os.path.join(self.tmp, "plain", "deep", "bin")
        os.makedirs(plain)
        py = os.path.join(plain, "python3")
        with open(py, "w") as fh:
            fh.write("#!/bin/sh\n")
        self.assertIsNone(venv_mod._venv_root_of(py))


class TestRequiredVenvTargets(unittest.TestCase):
    """`--reconcile` resolves only declared venvs, deduped, from the filesystem."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="tr-required-")
        self.base = os.path.join(self.tmp, "base")
        os.makedirs(os.path.join(self.base, "bin"))
        with open(os.path.join(self.base, "pyvenv.cfg"), "w") as fh:
            fh.write("home = /usr/bin\n")
        with open(os.path.join(self.base, "bin", "python3"), "w") as fh:
            fh.write("#!/bin/sh\n")
        os.chmod(os.path.join(self.base, "bin", "python3"), 0o755)
        for name in ("env-a", "env-b"):
            root = os.path.join(self.tmp, name)
            os.makedirs(os.path.join(root, "bin"))
            os.makedirs(os.path.join(root, "lib", "python3.14", "site-packages"))
            with open(os.path.join(root, "pyvenv.cfg"), "w") as fh:
                fh.write("home = ../base/bin\n")
            os.symlink(
                os.path.join(self.base, "bin", "python3"),
                os.path.join(root, "bin", "python3"),
            )

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    @unittest.skipIf(os.name == "nt", "symlink layout")
    def test_glob_resolves_and_dedupes(self):
        def fake_sp(py):
            return os.path.join(
                os.path.dirname(os.path.dirname(py)),
                "lib",
                "python3.14",
                "site-packages",
            )

        with patch.object(venv_mod, "_site_packages_of", side_effect=fake_sp):
            got = venv_mod.required_venv_targets(
                [os.path.join(self.tmp, "env-*"), os.path.join(self.tmp, "env-a")]
            )
        self.assertEqual(
            [v.root for v in got],
            [venv_mod._real(os.path.join(self.tmp, n)) for n in ("env-a", "env-b")],
        )

    def test_non_pep405_env_is_accepted_from_its_site_packages(self):
        """A uv-managed interpreter without pyvenv.cfg is still a target."""
        root = os.path.join(self.tmp, "baseonly")
        os.makedirs(os.path.join(root, "bin"))
        os.makedirs(os.path.join(root, "lib", "python3.14", "site-packages"))
        py = os.path.join(root, "bin", "python3")
        with open(py, "w") as fh:
            fh.write("#!/bin/sh\n")
        os.chmod(py, 0o755)

        def fake_sp(p):
            return os.path.join(
                os.path.dirname(os.path.dirname(p)), "lib", "python3.14", "site-packages"
            )

        with patch.object(venv_mod, "_site_packages_of", side_effect=fake_sp):
            got = venv_mod.required_venv_targets([root])
        self.assertEqual([v.root for v in got], [venv_mod._real(root)])


if __name__ == "__main__":
    unittest.main()
