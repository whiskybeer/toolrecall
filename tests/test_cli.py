"""Tests for CLI — commands dispatch correctly."""

import io
import os
import sys
import unittest
import tempfile
import shutil


class TestCLIMainDispatch(unittest.TestCase):
    """main() dispatches commands correctly from sys.argv."""

    def setUp(self):
        self.old_argv = sys.argv
        self.old_stdout = sys.stdout
        self.stdout = io.StringIO()
        sys.stdout = self.stdout

    def tearDown(self):
        sys.argv = self.old_argv
        sys.stdout = self.old_stdout

    def test_main_lists_all_registered_commands(self):
        """main() lists all registered commands."""
        expected = [
            "init",
            "status",
            "stats",
            "invalidate",
            "reset-stats",
            "index",
            "index-memory",
            "index-dir",
            "config-set",
            "serve",
            "nginx",
            "mcp",
            "daemon",
            "stop",
        ]
        sys.argv = ["toolrecall"]
        from toolrecall.cli import main

        main()
        output = self.stdout.getvalue()
        for cmd in expected:
            self.assertIn(cmd, output, f"Command '{cmd}' missing from help")

    def test_main_unknown_with_capital_shows_error(self):
        """Case-sensitive: 'Status' with capital is unknown, shows error."""
        sys.argv = ["toolrecall", "Status"]  # Not "status"
        from toolrecall.cli import main

        main()
        output = self.stdout.getvalue()
        self.assertIn("Unknown command", output)


# ═══════════════════════════════════════════════════════════
# Test: cmd_reset_stats
# ═══════════════════════════════════════════════════════════


class TestCLIResetStats(unittest.TestCase):
    """cmd_reset_stats() calls reset_stats without crash."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "test.db")
        os.environ["TOOLRECALL_CACHE_DB"] = self.db_path
        from toolrecall.cache import _init

        _init()
        self.old_stdout = sys.stdout
        self.stdout = io.StringIO()
        sys.stdout = self.stdout

    def tearDown(self):
        sys.stdout = self.old_stdout
        os.environ.pop("TOOLRECALL_CACHE_DB", None)
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_reset_stats_prints_confirmation(self):
        """reset-stats prints 'Cache statistics reset'."""
        sys.argv = ["toolrecall", "reset-stats"]
        from toolrecall.cli import cmd_reset_stats

        cmd_reset_stats()
        output = self.stdout.getvalue()
        self.assertIn("Cache statistics reset (hits/misses/tokens)", output)


# ═══════════════════════════════════════════════════════════
# Test: cmd_stop
# ═══════════════════════════════════════════════════════════


class TestCLIStop(unittest.TestCase):
    """cmd_stop() stops the daemon and reverts proxy base-URL wiring."""

    def setUp(self):
        self.old_argv = sys.argv
        self.old_stdout = sys.stdout
        self.stdout = io.StringIO()
        sys.stdout = self.stdout
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        sys.argv = self.old_argv
        sys.stdout = self.old_stdout
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_main_help_lists_stop(self):
        """main() help lists 'stop' as a command."""
        sys.argv = ["toolrecall"]
        from toolrecall.cli import main

        main()
        self.assertIn("stop", self.stdout.getvalue())

    def test_revert_removes_only_8569_lines(self):
        """Only :8569 localhost override lines are stripped; rest intact."""
        from toolrecall.cli import _revert_proxy_wiring_in_text

        text = (
            "export FOO=bar\n"
            "export OPENAI_BASE_URL=http://localhost:8569/v1\n"
            "export ANTHROPIC_BASE_URL=http://localhost:8569\n"
            "export OPENAI_BASE_URL=https://api.openai.com/v1\n"
            "export KEEP=me\n"
        )
        out = _revert_proxy_wiring_in_text("/home/me/.bashrc", text)
        self.assertNotIn("localhost:8569", out)  # both ToolRecall lines stripped
        self.assertIn("FOO=bar", out)  # unrelated kept
        self.assertIn("KEEP=me", out)  # unrelated kept
        # A real-host override (not localhost:8569) is kept:
        self.assertIn("https://api.openai.com", out)

    def test_cmd_stop_runs_without_crash(self):
        """cmd_stop() stops daemon + reverts wiring; does not raise."""
        from unittest.mock import patch
        from toolrecall.cli import cmd_stop

        # Use isolated fake wiring files so we never touch the real shell configs
        fake = {os.path.join(self.tmpdir, "bashrc"), os.path.join(self.tmpdir, "profile")}
        fake_text = "export OPENAI_BASE_URL=http://localhost:8569/v1\n"
        for p in fake:
            with open(p, "w") as f:
                f.write(fake_text)

        with (
            patch("toolrecall.cli._PROXY_WIRING_FILES", sorted(fake)),
            patch("toolrecall.cli._revert_proxy_wiring") as mock_revert,
        ):
            mock_revert.return_value = ["fake/reverted"]
            cmd_stop()

        output = self.stdout.getvalue()
        self.assertIn("ToolRecall Stop", output)


class TestCLIShimDisable(unittest.TestCase):
    """cmd_shim() --disable / --enable write/clear the persistent marker."""

    def setUp(self):
        self.old_argv = sys.argv
        self.old_stdout = sys.stdout
        self.stdout = io.StringIO()
        sys.stdout = self.stdout
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        sys.argv = self.old_argv
        sys.stdout = self.old_stdout
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _run_shim(self, flag: str):
        from unittest.mock import patch
        from toolrecall.cli import cmd_shim

        marker = os.path.join(self.tmpdir, "shim.disabled")
        sys.argv = ["toolrecall", "shim", flag, "--yes"]
        with patch("toolrecall.shim._marker_path", return_value=marker):
            cmd_shim()
        return marker

    def test_disable_writes_marker(self):
        from toolrecall.cli import cmd_shim  # noqa: F401 (ensures import works)

        marker = self._run_shim("--disable")
        self.assertTrue(os.path.isfile(marker))

    def test_enable_removes_marker(self):
        marker = self._run_shim("--disable")
        self.assertTrue(os.path.isfile(marker))
        self._run_shim("--enable")
        self.assertFalse(os.path.exists(marker))

    def test_disable_prints_confirmation(self):
        self._run_shim("--disable")
        self.assertIn("DISABLED", self.stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
