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
            "init", "status", "stats", "invalidate", "reset-stats",
            "index", "index-memory", "index-dir", "config-set",
            "serve", "nginx", "mcp", "daemon",
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


if __name__ == "__main__":
    unittest.main()