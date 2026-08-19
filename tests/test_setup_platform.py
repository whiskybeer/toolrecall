#!/usr/bin/env python3
"""Regression tests for the macOS / fresh-install bug report:
1. A relative TOOLRECALL_CACHE_DB (e.g. "cache.db") no longer crashes
   open_backend() with FileNotFoundError("") — the DB is created in cwd.
2. _host_os() classifies darwin/win32/linux correctly.
3. cmd_setup() on macOS installs a LaunchAgent (NOT a systemd unit).
"""

import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


class TestRelativeDbPath(unittest.TestCase):
    """open_backend must tolerate a bare filename as the cache DB path."""

    def test_relative_cache_db_creates_in_cwd(self):
        import tempfile

        from toolrecall.config import load_config
        from toolrecall.storage import open_backend

        with tempfile.TemporaryDirectory() as tmp:
            old_cwd = os.getcwd()
            os.chdir(tmp)
            try:
                with patch.dict(
                    os.environ,
                    {
                        "TOOLRECALL_CACHE_DB": "relative_cache.db",
                        "TOOLRECALL_STORAGE_BACKEND": "sqlite",
                    },
                ):
                    cfg = load_config()
                    conn = open_backend(cfg)
                    try:
                        conn.execute("CREATE TABLE IF NOT EXISTS probe(a INTEGER)")
                        conn.commit()
                    finally:
                        conn.close()
                self.assertTrue(
                    os.path.exists(os.path.join(tmp, "relative_cache.db")),
                    "relative DB should be created in the CWD",
                )
            finally:
                os.chdir(old_cwd)


class TestHostOs(unittest.TestCase):
    """_host_os() maps sys.platform to our OS classification."""

    def test_darwin(self):
        with patch("toolrecall.cli.sys.platform", "darwin"):
            from toolrecall.cli import _host_os

            self.assertEqual(_host_os(), "macos")

    def test_windows(self):
        with patch("toolrecall.cli.sys.platform", "win32"):
            from toolrecall.cli import _host_os

            self.assertEqual(_host_os(), "windows")

    def test_linux(self):
        with patch("toolrecall.cli.sys.platform", "linux"):
            from toolrecall.cli import _host_os

            self.assertEqual(_host_os(), "linux")


class TestMacOSLaunchAgent(unittest.TestCase):
    """The macOS LaunchAgent installer writes a real plist and reports success."""

    def test_installs_plist(self):
        import tempfile

        from toolrecall.cli import _install_macos_launch_agent

        with tempfile.TemporaryDirectory() as tmp:
            with (
                patch.dict(os.environ, {"HOME": tmp}),
                patch("shutil.which", return_value=sys.executable),
                patch("subprocess.run", return_value=None),
            ):
                ok, msg = _install_macos_launch_agent()
            self.assertTrue(ok, msg)
            plist = os.path.join(tmp, "Library", "LaunchAgents", "toolrecall-daemon.plist")
            self.assertTrue(os.path.exists(plist), "plist should be written")
            with open(plist) as f:
                content = f.read()
            self.assertIn("toolrecall-daemon", content)
            self.assertIn("RunAtLoad", content)
            self.assertIn("--foreground", content)

    def test_mac_setup_uses_launch_agent_not_systemd(self):
        """cmd_setup() on macOS must take the LaunchAgent branch, not systemd."""
        import io

        from toolrecall import cli as cli_mod

        with (
            patch("toolrecall.cli._host_os", return_value="macos"),
            patch(
                "toolrecall.cli._install_macos_launch_agent",
                return_value=(True, "macOS autostart: LaunchAgent installed (/x.plist)"),
            ) as mock_install,
            patch("toolrecall.cli.cmd_init"),
            patch.object(cli_mod, "_ensure_daemon", return_value=True),
            patch.object(cli_mod, "_ensure_agent_integration", return_value={}),
            patch("sys.stdout", new_callable=io.StringIO) as out,
        ):
            cli_mod.cmd_setup()
        output = out.getvalue()
        self.assertTrue(mock_install.called, "LaunchAgent install should be called on macOS")
        self.assertIn("LaunchAgent", output)
        self.assertNotIn("systemd", output.lower())


if __name__ == "__main__":
    unittest.main()
