"""Tests for agent profile auto-detection and setup in toolrecall.cli."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from toolrecall import cli


def _expand_to(home_dir: str):
    real_expand = os.path.expanduser

    def fake(p):
        if p == "~" or p.startswith("~/"):
            return os.path.join(home_dir, p[2:] if p.startswith("~/") else "")
        return real_expand(p)

    return patch.object(os.path, "expanduser", side_effect=fake)


class TestSetupAgentProfiles(unittest.TestCase):
    def test_claude_json_defaults_to_multiplexer_only(self):
        """When ~/.claude.json exists, ToolRecall registers with --multiplexer-only."""
        with tempfile.TemporaryDirectory() as home:
            claude_json = os.path.join(home, ".claude.json")
            with open(claude_json, "w") as f:
                json.dump({"mcpServers": {"existing": {"command": "echo"}}}, f)

            with (
                patch.object(sys, "argv", ["toolrecall", "setup", "--yes"]),
                patch.object(shutil, "which", return_value=None),
                _expand_to(home),
            ):
                result = cli._ensure_agent_integration()

            self.assertTrue(result.get("claude"))
            with open(claude_json) as f:
                cfg = json.load(f)
            self.assertIn("existing", cfg["mcpServers"])
            self.assertIn("toolrecall", cfg["mcpServers"])
            self.assertIn("--multiplexer-only", cfg["mcpServers"]["toolrecall"]["args"])

    def test_claude_interactive_allows_full_mode(self):
        """Interactive prompt with 'f' enables full bridge mode."""
        with tempfile.TemporaryDirectory() as home:
            fake_claude = os.path.join(home, "bin", "claude")
            os.makedirs(os.path.dirname(fake_claude), exist_ok=True)
            open(fake_claude, "w").close()

            def fake_which(name):
                return fake_claude if name == "claude" else None

            with (
                patch.object(sys, "argv", ["toolrecall", "setup"]),
                patch.object(shutil, "which", side_effect=fake_which),
                patch.dict(os.environ, {}, clear=True),
                patch("builtins.input", return_value="f"),
                patch.object(
                    subprocess, "run", return_value=subprocess.CompletedProcess([], 0)
                ) as mock_run,
                _expand_to(home),
            ):
                result = cli._ensure_agent_integration()

            self.assertTrue(result.get("claude"))
            cmd = mock_run.call_args[0][0]
            self.assertNotIn("--multiplexer-only", cmd)

    def test_cline_detected_via_config_dir(self):
        """Cline detected via ~/.config/cline creates merge-preserving config."""
        with tempfile.TemporaryDirectory() as home:
            cline_dir = os.path.join(home, ".config", "cline")
            os.makedirs(cline_dir, exist_ok=True)
            mcp_file = os.path.join(cline_dir, "mcp_settings.json")
            with open(mcp_file, "w") as f:
                json.dump({"mcpServers": {"other_server": {"command": "node"}}}, f)

            with (
                patch.object(sys, "argv", ["toolrecall", "setup", "--yes"]),
                patch.object(shutil, "which", return_value=None),
                _expand_to(home),
            ):
                result = cli._ensure_agent_integration()

            self.assertTrue(result.get("cline"))
            with open(mcp_file) as f:
                cfg = json.load(f)
            self.assertIn("other_server", cfg["mcpServers"])
            self.assertIn("toolrecall", cfg["mcpServers"])
            self.assertEqual(cfg["mcpServers"]["toolrecall"]["args"], ["mcp"])

    def test_cline_detected_via_vscode_storage(self):
        """Cline detected via VS Code extension global storage."""
        with tempfile.TemporaryDirectory() as home:
            storage_dir = os.path.join(
                home,
                ".config",
                "Code",
                "User",
                "globalStorage",
                "saoudrizwan.claude-dev",
                "settings",
            )
            os.makedirs(storage_dir, exist_ok=True)
            target = os.path.join(storage_dir, "cline_mcp_settings.json")

            with (
                patch.object(sys, "argv", ["toolrecall", "setup", "--yes"]),
                patch.object(shutil, "which", return_value=None),
                _expand_to(home),
            ):
                result = cli._ensure_agent_integration()

            self.assertTrue(result.get("cline"))
            self.assertTrue(os.path.exists(target))
            with open(target) as f:
                cfg = json.load(f)
            self.assertIn("toolrecall", cfg["mcpServers"])

    def test_cursor_detected_via_cursor_dir(self):
        """Cursor detected via ~/.cursor creates config with --multiplexer-only."""
        with tempfile.TemporaryDirectory() as home:
            cursor_dir = os.path.join(home, ".cursor")
            os.makedirs(cursor_dir, exist_ok=True)
            mcp_file = os.path.join(cursor_dir, "mcp.json")

            with (
                patch.object(sys, "argv", ["toolrecall", "setup", "--yes"]),
                patch.object(shutil, "which", return_value=None),
                _expand_to(home),
            ):
                result = cli._ensure_agent_integration()

            self.assertTrue(result.get("cursor"))
            with open(mcp_file) as f:
                cfg = json.load(f)
            self.assertIn("toolrecall", cfg["mcpServers"])
            self.assertIn("--multiplexer-only", cfg["mcpServers"]["toolrecall"]["args"])

    def test_cursor_detected_via_binary(self):
        """Cursor detected via binary path."""
        with tempfile.TemporaryDirectory() as home:

            def fake_which(name):
                return "/usr/local/bin/cursor" if name == "cursor" else None

            with (
                patch.object(sys, "argv", ["toolrecall", "setup", "--yes"]),
                patch.object(shutil, "which", side_effect=fake_which),
                _expand_to(home),
            ):
                result = cli._ensure_agent_integration()

            self.assertTrue(result.get("cursor"))
            mcp_file = os.path.join(home, ".cursor", "mcp.json")
            self.assertTrue(os.path.exists(mcp_file))
            with open(mcp_file) as f:
                cfg = json.load(f)
            self.assertIn("--multiplexer-only", cfg["mcpServers"]["toolrecall"]["args"])

    def test_claude_interactive_allows_skip(self):
        """Interactive prompt with 's' skips registration."""
        with tempfile.TemporaryDirectory() as home:
            fake_claude = os.path.join(home, "bin", "claude")
            os.makedirs(os.path.dirname(fake_claude), exist_ok=True)
            open(fake_claude, "w").close()

            def fake_which(name):
                return fake_claude if name == "claude" else None

            with (
                patch.object(sys, "argv", ["toolrecall", "setup"]),
                patch.object(shutil, "which", side_effect=fake_which),
                patch.dict(os.environ, {}, clear=True),
                patch("builtins.input", return_value="s"),
                _expand_to(home),
            ):
                result = cli._ensure_agent_integration()

            self.assertFalse(result.get("claude"))

    def test_claude_remote_http_defaults_to_skip(self):
        """When only remote HTTP MCPs are configured, default prompt choice skips."""
        with tempfile.TemporaryDirectory() as home:
            fake_claude = os.path.join(home, "bin", "claude")
            os.makedirs(os.path.dirname(fake_claude), exist_ok=True)
            open(fake_claude, "w").close()

            claude_json = os.path.join(home, ".claude.json")
            with open(claude_json, "w") as f:
                json.dump({"mcpServers": {"remote_mcp": {"url": "https://example.com/mcp"}}}, f)

            def fake_which(name):
                return fake_claude if name == "claude" else None

            with (
                patch.object(sys, "argv", ["toolrecall", "setup"]),
                patch.object(shutil, "which", side_effect=fake_which),
                patch.dict(os.environ, {}, clear=True),
                patch("builtins.input", return_value=""),  # default enter
                _expand_to(home),
            ):
                result = cli._ensure_agent_integration()

            self.assertFalse(result.get("claude"))

    def test_doctor_claude_output(self):
        """toolrecall doctor claude runs cleanly and outputs diagnostics."""
        import io
        from contextlib import redirect_stdout

        with tempfile.TemporaryDirectory() as home:
            fake_claude = os.path.join(home, "bin", "claude")
            os.makedirs(os.path.dirname(fake_claude), exist_ok=True)
            open(fake_claude, "w").close()

            claude_json = os.path.join(home, ".claude.json")
            with open(claude_json, "w") as f:
                json.dump(
                    {
                        "mcpServers": {
                            "remote1": {"url": "https://remote.mcp"},
                            "local1": {"command": "node", "args": ["server.js"]},
                        }
                    },
                    f,
                )

            def fake_which(name):
                return fake_claude if name == "claude" else None

            buf = io.StringIO()
            with (
                patch.object(shutil, "which", side_effect=fake_which),
                patch.object(
                    subprocess,
                    "run",
                    return_value=subprocess.CompletedProcess([], 0, stdout="2.1.306\n"),
                ),
                _expand_to(home),
                redirect_stdout(buf),
            ):
                cli._diagnose_claude_code()

            out = buf.getvalue()
            self.assertIn("ToolRecall Compatibility Audit: Claude Code", out)
            self.assertIn("Local stdio:   1 server(s)", out)
            self.assertIn("Remote HTTP:   1 server(s)", out)


if __name__ == "__main__":
    unittest.main()
