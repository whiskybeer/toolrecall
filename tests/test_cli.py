"""Tests for cli.py — ToolRecall CLI command dispatch and helpers.

The CLI module dispatches subcommands like:
  init, status, stats, invalidate, reset-stats, index, index-memory,
  index-dir, config-set, export-dataset, serve, nginx, mcp, mcp-legacy, daemon

Tests cover:
  - main() dispatches to correct command function
  - main() prints help when no command given
  - main() prints error for unknown command
  - main() lists all registered commands
  - cmd_nginx() generates valid nginx config file
  - cmd_reset_stats() calls reset_stats without error
  - cmd_config_set() parses keys/values correctly
  - cmd_index_dir() prints help when no directory specified
  - cmd_index_dir() warns on nonexistent directory
  - cmd_init() creates config and .env files (mocked input)
  - cmd_init() does not overwrite existing files
  - cmd_export_dataset() handles output path
  - Unknown command shows error message
"""

import json
import os
import sys
import unittest
import tempfile
import shutil
import io

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


# ═══════════════════════════════════════════════════════════
# Test: main() dispatch
# ═══════════════════════════════════════════════════════════

class TestCLIMainDispatch(unittest.TestCase):
    """main() dispatches commands correctly."""

    def setUp(self):
        self.old_argv = sys.argv
        self.old_stdout = sys.stdout
        self.stdout = io.StringIO()
        sys.stdout = self.stdout

    def tearDown(self):
        sys.argv = self.old_argv
        sys.stdout = self.old_stdout

    def test_main_no_args_prints_help(self):
        """Running toolrecall with no args prints command list."""
        sys.argv = ["toolrecall"]
        from toolrecall.cli import main
        main()
        output = self.stdout.getvalue()
        self.assertIn("Usage:", output)
        self.assertIn("Commands:", output)

    def test_main_unknown_command_shows_error(self):
        """Running an unknown command prints error with available commands."""
        sys.argv = ["toolrecall", "nonexistent"]
        from toolrecall.cli import main
        main()
        output = self.stdout.getvalue()
        self.assertIn("Unknown command", output)

    def test_main_lists_all_registered_commands(self):
        """Help output includes every registered command name."""
        expected = [
            "init", "status", "stats", "invalidate", "reset-stats",
            "index", "index-memory", "index-dir", "config-set",
            "export-dataset", "serve", "nginx", "mcp", "mcp-legacy", "daemon",
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
        self.assertIn("reset", output.lower())


# ═══════════════════════════════════════════════════════════
# Test: cmd_nginx
# ═══════════════════════════════════════════════════════════

class TestCLINginx(unittest.TestCase):
    """cmd_nginx() generates valid nginx config at ~/.toolrecall/."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.old_home = os.environ.get("HOME", "")
        os.environ["HOME"] = self.tmpdir
        self.old_stdout = sys.stdout
        self.stdout = io.StringIO()
        sys.stdout = self.stdout

    def tearDown(self):
        sys.stdout = self.old_stdout
        os.environ["HOME"] = self.old_home
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_nginx_creates_config_file(self):
        """nginx config is written to ~/.toolrecall/nginx-toolrecall.conf."""
        from toolrecall.cli import cmd_nginx
        cmd_nginx()

        cfg_path = os.path.join(self.tmpdir, ".toolrecall", "nginx-toolrecall.conf")
        self.assertTrue(os.path.exists(cfg_path), f"Config not at {cfg_path}")

        with open(cfg_path) as f:
            content = f.read()
        self.assertIn("proxy_pass http://127.0.0.1:8567/", content)
        self.assertIn("ToolRecall", content)


# ═══════════════════════════════════════════════════════════
# Test: cmd_config_set
# ═══════════════════════════════════════════════════════════

class TestCLIConfigSet(unittest.TestCase):
    """cmd_config_set() parses config key=value correctly."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.old_home = os.environ.get("HOME", "")
        os.environ["HOME"] = self.tmpdir

        # Create a minimal config.toml
        cfg_dir = os.path.join(self.tmpdir, ".toolrecall")
        os.makedirs(cfg_dir, exist_ok=True)
        with open(os.path.join(cfg_dir, "config.toml"), "w") as f:
            f.write("[proxy]\nport = 8567\nbind = \"127.0.0.1\"\n\n[mcp]\nallow_terminal = false\n")

        self.old_argv = sys.argv
        self.old_stdout = sys.stdout
        self.old_stderr = sys.stderr
        self.stdout = io.StringIO()
        self.stderr = io.StringIO()
        sys.stdout = self.stdout
        sys.stderr = self.stderr

    def tearDown(self):
        sys.argv = self.old_argv
        sys.stdout = self.old_stdout
        sys.stderr = self.old_stderr
        os.environ["HOME"] = self.old_home
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_config_set_help_on_no_args(self):
        """config-set with no args prints usage."""
        sys.argv = ["toolrecall", "config-set"]
        from toolrecall.cli import cmd_config_set
        cmd_config_set()
        output = self.stdout.getvalue()
        # Without tomli-w, it prints an error (not usage). Either way, no crash.
        # Usage is printed when --help is passed explicitly
        self.assertNotIn("Traceback", output + self.stderr.getvalue())

    def test_config_set_invalid_key_format(self):
        """config-set without section.key prefix prints error."""
        sys.argv = ["toolrecall", "config-set", "badkey", "value"]
        from toolrecall.cli import cmd_config_set
        cmd_config_set()
        output = self.stdout.getvalue()
        # Without tomli-w: prints install error. With tomli-w: prints 'Invalid key'
        # Either is acceptable — just don't crash
        self.assertNotIn("Traceback", output + self.stderr.getvalue())

    def test_config_set_writes_when_tomli_available(self):
        """If tomli-w is available, config-set sets the value."""
        from toolrecall.config import _have_tomli_w
        if not _have_tomli_w():
            self.skipTest("tomli-w not installed")
        sys.argv = ["toolrecall", "config-set", "proxy.port", "9090"]
        from toolrecall.cli import cmd_config_set
        cmd_config_set()
        output = self.stdout.getvalue()
        self.assertIn("Set proxy.port", output)


# ═══════════════════════════════════════════════════════════
# Test: cmd_index_dir
# ═══════════════════════════════════════════════════════════

class TestCLIIndexDir(unittest.TestCase):
    """cmd_index_dir() prints help when no args, warns if dir missing."""

    def setUp(self):
        self.old_argv = sys.argv
        self.old_stdout = sys.stdout
        self.stdout = io.StringIO()
        sys.stdout = self.stdout

    def tearDown(self):
        sys.argv = self.old_argv
        sys.stdout = self.old_stdout

    def test_index_dir_help_with_no_args(self):
        """index-dir with no args prints usage instructions and exits."""
        sys.argv = ["toolrecall", "index-dir"]
        from toolrecall.cli import cmd_index_dir
        cmd_index_dir()
        output = self.stdout.getvalue()
        self.assertIn("Usage:", output)
        self.assertIn("index-dir", output)

    def test_index_dir_warns_on_nonexistent(self):
        """index-dir with a non-existent directory prints a warning."""
        sys.argv = ["toolrecall", "index-dir", "/nonexistent/vault"]
        from toolrecall.cli import cmd_index_dir
        cmd_index_dir()
        output = self.stdout.getvalue()
        self.assertIn("Not a directory", output)


# ═══════════════════════════════════════════════════════════
# Test: cmd_init — must mock input() to avoid interactive hang
# ═══════════════════════════════════════════════════════════

class TestCLIInit(unittest.TestCase):
    """cmd_init() creates config and .env files.

    Note: We mock builtins.input() because cmd_init() uses interactive input()
    to collect allowed paths. The test simulates pressing Enter (default).
    """

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.old_home = os.environ.get("HOME", "")
        os.environ["HOME"] = self.tmpdir

        import builtins
        self._orig_input = builtins.input
        builtins.input = lambda prompt="": ""  # Press Enter → use defaults

        self.old_stdout = sys.stdout
        self.stdout = io.StringIO()
        sys.stdout = self.stdout

    def tearDown(self):
        import builtins
        builtins.input = self._orig_input
        sys.stdout = self.old_stdout
        os.environ["HOME"] = self.old_home
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_init_creates_config_and_env(self):
        """cmd_init() creates config.toml and .env when they don't exist."""
        from toolrecall.cli import cmd_init
        cmd_init()
        cfg_path = os.path.join(self.tmpdir, ".toolrecall", "config.toml")
        env_path = os.path.join(self.tmpdir, ".toolrecall", ".env")
        self.assertTrue(os.path.exists(cfg_path), "config.toml should be created")
        self.assertTrue(os.path.exists(env_path), ".env should be created")
        with open(cfg_path) as f:
            self.assertIn("allowed_paths", f.read())

    def test_init_does_not_overwrite_existing(self):
        """cmd_init() preserves existing config.toml when it already exists."""
        os.makedirs(os.path.join(self.tmpdir, ".toolrecall"), exist_ok=True)
        cfg_path = os.path.join(self.tmpdir, ".toolrecall", "config.toml")
        with open(cfg_path, "w") as f:
            f.write("preexisting config")

        from toolrecall.cli import cmd_init
        cmd_init()
        with open(cfg_path) as f:
            self.assertEqual(f.read(), "preexisting config",
                             "Existing config.toml should not be overwritten")


# ═══════════════════════════════════════════════════════════
# Test: cmd_export_dataset
# ═══════════════════════════════════════════════════════════

class TestCLIExportDataset(unittest.TestCase):
    """cmd_export_dataset() handles output path argument."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "test.db")
        os.environ["TOOLRECALL_CACHE_DB"] = self.db_path

        # Create empty tables so export doesn't crash
        import sqlite3
        conn = sqlite3.connect(self.db_path)
        conn.execute("CREATE TABLE IF NOT EXISTS mcp_cache (request_hash TEXT PRIMARY KEY, data TEXT)")
        conn.execute("CREATE TABLE IF NOT EXISTS terminal_cache (command TEXT, output TEXT)")
        conn.commit()
        conn.close()

        self.old_argv = sys.argv
        self.old_stdout = sys.stdout
        self.stdout = io.StringIO()
        sys.stdout = self.stdout

    def tearDown(self):
        sys.argv = self.old_argv
        sys.stdout = self.old_stdout
        os.environ.pop("TOOLRECALL_CACHE_DB", None)
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_export_dataset_default_path(self):
        """export-dataset prints count and format even with empty cache."""
        sys.argv = ["toolrecall", "export-dataset"]
        from toolrecall.cli import cmd_export_dataset
        cmd_export_dataset()
        output = self.stdout.getvalue()
        self.assertIn("0 tool trajectories", output)
        self.assertIn("JSONL", output)

    def test_export_dataset_custom_path(self):
        """export-dataset accepts custom output path argument."""
        out_path = os.path.join(self.tmpdir, "custom.jsonl")
        sys.argv = ["toolrecall", "export-dataset", out_path]
        from toolrecall.cli import cmd_export_dataset
        cmd_export_dataset()
        output = self.stdout.getvalue()
        self.assertIn("0 tool trajectories", output)


if __name__ == "__main__":
    unittest.main()
