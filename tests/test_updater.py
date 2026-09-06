"""Tests for toolrecall.updater — opt-out auto-updater (default ON)."""

import json
import os
import sys
import tempfile
import time
import unittest
from contextlib import contextmanager
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from toolrecall import updater  # noqa: E402


@contextmanager
def _update_check_env(value=None):
    """Run with TOOLRECALL_UPDATE_CHECK removed (value=None) or set.

    conftest sets TOOLRECALL_UPDATE_CHECK=0 suite-wide; tests that exercise
    the enabled path pop it (patch.dict restores on exit).
    """
    with patch.dict(os.environ):
        if value is None:
            os.environ.pop("TOOLRECALL_UPDATE_CHECK", None)
        else:
            os.environ["TOOLRECALL_UPDATE_CHECK"] = value
        yield


class _StateIsolated(unittest.TestCase):
    """Point the updater's state file at a per-test temp path."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.state_path = os.path.join(self._tmp.name, "state.json")
        env = patch.dict(
            os.environ,
            {"TOOLRECALL_UPDATE_STATE": self.state_path, "TOOLRECALL_UPDATE_CHECK": "1"},
        )
        env.start()
        self.addCleanup(env.stop)

    def _state(self):
        with open(self.state_path) as f:
            return json.load(f)


class TestVersionKey(unittest.TestCase):
    """Dependency-free semver compare (plan Task 1)."""

    def test_ordering(self):
        from toolrecall.updater import _version_key

        self.assertLess(_version_key("0.8.19"), _version_key("0.9.0"))
        self.assertLess(_version_key("0.8.9"), _version_key("0.8.10"))
        self.assertLess(_version_key("1.0.0rc1"), _version_key("1.0.0"))
        self.assertLess(_version_key("0.8.19"), _version_key("0.8.20"))
        self.assertEqual(_version_key("0.8.19"), _version_key("0.8.19"))

    def test_garbage_does_not_crash(self):
        from toolrecall.updater import _version_key

        self.assertIsInstance(_version_key("not-a-version"), tuple)


class TestCheckForUpdate(_StateIsolated):
    """Interval-gated PyPI check (plan Task 2)."""

    def _cfg(self, enabled=True, interval=24):
        import toolrecall.config as config_mod

        cfg = config_mod.Config.__new__(config_mod.Config)
        cfg._data = {"update": {"enabled": enabled, "check_interval_hours": interval}}
        return cfg

    def test_disabled_returns_none_no_network(self):
        with _update_check_env("0"), patch.object(updater, "_pypi_latest") as p:
            r = updater.check_for_update(self._cfg(enabled=False), force=True)
        self.assertIsNone(r)
        p.assert_not_called()

    def test_within_interval_no_network(self):
        hour_ago = time.time() - 3600  # 1h < 24h interval
        with open(self.state_path, "w") as f:
            json.dump({"last_check": hour_ago}, f)
        with patch.object(updater, "_pypi_latest") as p:
            r = updater.check_for_update(self._cfg(), force=False)
        self.assertIsNone(r)
        p.assert_not_called()
        # state last_check is NOT refreshed (no network fired)
        self.assertEqual(self._state()["last_check"], hour_ago)

    def test_force_bypasses_interval(self):
        with patch.object(updater, "_pypi_latest", return_value=updater._VERSION):
            r = updater.check_for_update(self._cfg(), force=True)
        self.assertIsNone(r)  # same version → no update available
        self.assertIn("last_check", self._state())

    def test_newer_version_returned(self):
        with patch.object(updater, "_pypi_latest", return_value="9.9.9"):
            r = updater.check_for_update(self._cfg(), force=True)
        self.assertEqual(r, "9.9.9")

    def test_pypi_failure_recorded_not_silent(self):
        """PyPI failure → None, but state records the failed attempt (auditable)."""
        with patch.object(updater, "_pypi_latest", return_value=None):
            r = updater.check_for_update(self._cfg(), force=True)
        self.assertIsNone(r)
        self.assertTrue(self._state().get("last_check_failed"))

    def test_interval_honored_after_failure(self):
        """A failed check still counts as 'checked' — no hot-loop retries."""
        with patch.object(updater, "_pypi_latest", return_value=None):
            updater.check_for_update(self._cfg(), force=True)
        with patch.object(updater, "_pypi_latest") as p:
            updater.check_for_update(self._cfg(), force=False)
        p.assert_not_called()


class TestApplyUpdate(_StateIsolated):
    """pip/uv upgrade with editable-guard (plan Task 3)."""

    def test_editable_install_never_upgrades(self):
        with (
            patch.object(updater, "_is_editable_install", return_value=True),
            patch.object(updater.subprocess, "run") as run,
        ):
            ok = updater.apply_update("9.9.9")
        self.assertFalse(ok)
        run.assert_not_called()
        # refusal is recorded, not swallowed
        self.assertEqual(self._state().get("last_update_status"), "skipped_editable")

    def test_pip_failure_recorded(self):
        """pip exit ≠ 0 → False, failure recorded (visible via status)."""
        proc = updater.subprocess.CompletedProcess([], 1)
        with (
            patch.object(updater, "_is_editable_install", return_value=False),
            patch.object(updater.subprocess, "run", return_value=proc),
        ):
            ok = updater.apply_update("9.9.9")
        self.assertFalse(ok)
        self.assertEqual(self._state().get("last_update_status"), "failed")

    def test_pip_success_recorded(self):
        proc = updater.subprocess.CompletedProcess([], 0)
        with (
            patch.object(updater, "_is_editable_install", return_value=False),
            patch.object(updater.subprocess, "run", return_value=proc) as run,
        ):
            ok = updater.apply_update("9.9.9")
        self.assertTrue(ok)
        self.assertEqual(self._state().get("last_update_status"), "updated")
        # upgrade targets THIS interpreter's pip module
        args = run.call_args[0][0]
        self.assertIn("-m", args)
        self.assertIn("pip", args)
        self.assertIn("toolrecall", args)

    def test_pip_exception_recorded(self):
        with (
            patch.object(updater, "_is_editable_install", return_value=False),
            patch.object(updater.subprocess, "run", side_effect=OSError("no pip")),
        ):
            ok = updater.apply_update("9.9.9")
        self.assertFalse(ok)
        self.assertEqual(self._state().get("last_update_status"), "failed")


class TestConfigUpdateKeys(unittest.TestCase):
    """[update] section: default ON, TOML off, env override wins."""

    def _cfg(self, data):
        import toolrecall.config as config_mod

        cfg = config_mod.Config.__new__(config_mod.Config)
        cfg._data = data
        return cfg

    def test_default_enabled(self):
        with _update_check_env():  # pop conftest's =0 so the default applies
            cfg = self._cfg({})
            self.assertTrue(cfg.update_enabled)
            self.assertEqual(cfg.update_check_interval_hours, 24.0)

    def test_toml_disable(self):
        cfg = self._cfg({"update": {"enabled": False, "check_interval_hours": 7}})
        self.assertFalse(cfg.update_enabled)
        self.assertEqual(cfg.update_check_interval_hours, 7.0)

    def test_env_override_wins(self):
        # env=1 forces ON even when TOML says false
        cfg = self._cfg({"update": {"enabled": False}})
        with patch.dict(os.environ, {"TOOLRECALL_UPDATE_CHECK": "1"}):
            self.assertTrue(cfg.update_enabled)


class TestCliHook(unittest.TestCase):
    """main() fires the updater only when enabled; skip-list honored."""

    def test_main_skips_check_when_disabled(self):
        """TOOLRECALL_UPDATE_CHECK=0 → check_for_update never fires (conftest default)."""
        import io
        import toolrecall.cli as cli

        buf = io.StringIO()
        argv = patch.object(sys, "argv", ["toolrecall", "--version"])
        out = patch.object(sys, "stdout", buf)
        with _update_check_env("0"), argv, out, patch.object(updater, "check_for_update") as chk:
            cli.main()
        chk.assert_not_called()

    def test_main_calls_check_when_enabled(self):
        import io
        import toolrecall.cli as cli

        buf = io.StringIO()
        argv = patch.object(sys, "argv", ["toolrecall", "--version"])
        out = patch.object(sys, "stdout", buf)
        with (
            _update_check_env(),
            argv,
            out,
            patch.object(updater, "check_for_update", return_value=None) as chk,
        ):
            cli.main()
        chk.assert_called_once()

    def test_daemon_command_never_checks(self):
        import io
        import toolrecall.cli as cli

        buf = io.StringIO()
        argv = patch.object(sys, "argv", ["toolrecall", "daemon", "--status"])
        out = patch.object(sys, "stdout", buf)
        with _update_check_env(), argv, out, patch.object(updater, "check_for_update") as chk:
            try:
                cli._maybe_auto_update()
            except SystemExit:
                pass
        chk.assert_not_called()

    def test_hook_applies_update_when_newer(self):
        import io
        import toolrecall.cli as cli

        buf = io.StringIO()
        argv = patch.object(sys, "argv", ["toolrecall", "status"])
        out = patch.object(sys, "stdout", buf)
        with (
            _update_check_env(),
            argv,
            out,
            patch.object(updater, "check_for_update", return_value="9.9.9") as chk,
            patch.object(updater, "apply_update", return_value=True) as app,
            patch.object(sys.modules["toolrecall.config"], "load_config"),
        ):
            cli._maybe_auto_update()
        chk.assert_called_once()
        app.assert_called_once_with("9.9.9")
        self.assertIn("Updated", buf.getvalue())


class TestPipxDetection(unittest.TestCase):
    """_pip_cmd() prefers the pipx manager inside pipx venvs; pip elsewhere."""

    def test_non_pipx_env_uses_pip(self):
        # Repo/plain venv: prefix not under pipx venvs root → naked pip
        with patch.object(updater, "_pipx_venv_root", return_value=None):
            self.assertEqual(updater._pip_cmd(), [sys.executable, "-m", "pip"])

    def test_pipx_env_prefers_pipx_upgrade(self):
        with (
            patch.object(updater, "_pipx_venv_root", return_value="/home/u/.local/pipx/venvs"),
            patch("shutil.which", return_value="/usr/bin/pipx"),
        ):
            self.assertEqual(updater._pip_cmd(), ["/usr/bin/pipx", "upgrade", "toolrecall"])

    def test_pipx_env_falls_back_to_pip_without_pipx_binary(self):
        # pipx dir removed from PATH but we still run in its venv → pip
        with (
            patch.object(updater, "_pipx_venv_root", return_value="/home/u/.local/pipx/venvs"),
            patch("shutil.which", return_value=None),
        ):
            self.assertEqual(updater._pip_cmd(), [sys.executable, "-m", "pip"])

    def test_detection_matches_real_pipx_layout(self):
        # sys.prefix directly under ~/.local/pipx/venvs/<pkg> is detected;
        # the root itself or an unrelated prefix is not.
        with tempfile.TemporaryDirectory() as tmp:
            root = os.path.join(tmp, ".local", "pipx", "venvs")
            pkg = os.path.join(root, "toolrecall")
            os.makedirs(pkg)
            with patch.object(updater.sys, "prefix", pkg), patch.dict(os.environ, {"HOME": tmp}):
                self.assertEqual(updater._pipx_venv_root(), root)
            with patch.object(updater.sys, "prefix", root), patch.dict(os.environ, {"HOME": tmp}):
                self.assertIsNone(updater._pipx_venv_root())
            with (
                patch.object(updater.sys, "prefix", os.path.join(tmp, "elsewhere")),
                patch.dict(os.environ, {"HOME": tmp}),
            ):
                self.assertIsNone(updater._pipx_venv_root())

    def test_custom_pipx_home_respected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = os.path.join(tmp, "custom-pipx", "venvs")
            pkg = os.path.join(root, "toolrecall")
            os.makedirs(pkg)
            with (
                patch.object(updater.sys, "prefix", pkg),
                patch.dict(os.environ, {"PIPX_LOCAL_VENVS": root}),
            ):
                self.assertEqual(updater._pipx_venv_root(), root)


if __name__ == "__main__":
    unittest.main()
