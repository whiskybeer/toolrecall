"""Tests for CLI — commands dispatch correctly."""

import io
import json
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
        # Save-restore (never bare pop): popping re-exposes the PRODUCTION
        # default path for the rest of the process — any later config
        # resolution before the autouse fixture re-pins hits ~/.toolrecall
        # (the 2026-09-10 wipe-storm window).
        self._orig_cache_db = os.environ.get("TOOLRECALL_CACHE_DB")
        os.environ["TOOLRECALL_CACHE_DB"] = self.db_path
        from toolrecall.cache import _init

        _init()
        self.old_stdout = sys.stdout
        self.stdout = io.StringIO()
        sys.stdout = self.stdout

    def tearDown(self):
        sys.stdout = self.old_stdout
        if self._orig_cache_db is None:
            os.environ.pop("TOOLRECALL_CACHE_DB", None)
        else:
            os.environ["TOOLRECALL_CACHE_DB"] = self._orig_cache_db
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_reset_stats_prints_confirmation(self):
        """reset-stats --yes prints the confirmation, and it must not promise a wipe.

        The command moves the statistics window into banked lifetime totals
        (2026-10-22); a message that still says "reset" without saying "banked"
        would leave the operator believing history was thrown away.
        """
        sys.argv = ["toolrecall", "reset-stats", "--yes"]
        from toolrecall.cli import cmd_reset_stats

        cmd_reset_stats()
        output = self.stdout.getvalue()
        self.assertIn("Statistics window reset", output)
        self.assertIn("banked lifetime values", output)

    def test_reset_stats_help_does_not_promise_a_wipe(self):
        """--help must describe the move, not a destructive reset."""
        sys.argv = ["toolrecall", "reset-stats", "--help"]
        from toolrecall.cli import cmd_reset_stats

        cmd_reset_stats()
        output = self.stdout.getvalue()
        self.assertIn("nothing is deleted", output)
        self.assertNotIn("Wipe", output)


class TestCLIStatsContextFlow(unittest.TestCase):
    """cmd_stats embeds a context_flow block; fail-soft when daemon is down.

    The 2026-09-15 cron misread: an armed flow with 593k banked tokens was
    reported inactive because armed/checkpoint state was invisible in the
    stats JSON. cmd_stats must always carry the live tracker snapshot (or
    an explicit reason) so reporters can render the verdict instead of
    guessing from a bare counter.
    """

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "test.db")
        # Save-restore (never bare pop): see TestCLIResetStats.setUp.
        self._orig_cache_db = os.environ.get("TOOLRECALL_CACHE_DB")
        os.environ["TOOLRECALL_CACHE_DB"] = self.db_path
        from toolrecall.cache import _init

        _init()
        self.old_stdout = sys.stdout
        self.stdout = io.StringIO()
        sys.stdout = self.stdout

    def tearDown(self):
        sys.stdout = self.old_stdout
        if self._orig_cache_db is None:
            os.environ.pop("TOOLRECALL_CACHE_DB", None)
        else:
            os.environ["TOOLRECALL_CACHE_DB"] = self._orig_cache_db
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_stats_json_includes_context_flow_block(self):
        """stats JSON always carries context_flow {available, armed, ...}."""
        sys.argv = ["toolrecall", "stats"]
        from toolrecall.cli import cmd_stats

        cmd_stats()
        data = json.loads(self.stdout.getvalue())
        self.assertIn("context_flow", data)
        cf = data["context_flow"]
        for key in ("available", "armed", "checkpoint", "banked_tokens", "reason"):
            self.assertIn(key, cf)

    def test_context_flow_fail_soft_without_daemon(self):
        """No daemon on the pinned hermetic socket → available=False with a
        reason, never a raise and never a fake armed=True."""
        sys.argv = ["toolrecall", "stats"]
        from toolrecall.cli import cmd_stats

        cmd_stats()
        data = json.loads(self.stdout.getvalue())
        cf = data["context_flow"]
        self.assertFalse(cf["available"])
        self.assertFalse(cf["armed"])
        self.assertTrue(cf["reason"], "degraded snapshot must explain why")


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

        # CRITICAL hermeticity: cmd_stop() → daemon.stop_daemon() stops the
        # LIVE production daemon — first via `systemctl --user stop`, then
        # (if that "fails") via SIGTERM to the PID in ~/.toolrecall/daemon.pid,
        # then unlinks the real socket. Both paths must be inert here.
        # NOTE: stubbing only subprocess.run is NOT enough — the stubbed rc=1
        # makes stop_daemon() fall through to the PID-file path, which kills
        # the prod daemon anyway (observed 2026-09-09 21:03:47 during a
        # pytest of test_cli.py, and again while verifying this fix).
        # Patch the whole stop_daemon function instead.
        with (
            patch("toolrecall.cli._PROXY_WIRING_FILES", sorted(fake)),
            patch("toolrecall.cli._revert_proxy_wiring") as mock_revert,
            patch("toolrecall.daemon.stop_daemon") as mock_stop,
        ):
            mock_revert.return_value = ["fake/reverted"]
            cmd_stop()

        mock_stop.assert_called_once()
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
