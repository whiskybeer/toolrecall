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


class TestDoctorClaude(unittest.TestCase):
    """`toolrecall doctor claude` verdicts across real-world Claude Code setups."""

    def _run(self, claude_json=None, repo_files=None, args=(), env=None, local_mcp=None):
        import io
        from contextlib import redirect_stdout

        with tempfile.TemporaryDirectory() as tmp:
            home = os.path.join(tmp, "home")
            repo = os.path.realpath(os.path.join(tmp, "repo"))
            os.makedirs(home)
            os.makedirs(os.path.join(repo, ".git"))
            fake_claude = os.path.join(tmp, "claude")
            open(fake_claude, "w").close()
            if local_mcp is not None:
                claude_json = {"projects": {repo: {"mcpServers": local_mcp}}}
            if claude_json is not None:
                with open(os.path.join(home, ".claude.json"), "w") as f:
                    f.write(
                        claude_json if isinstance(claude_json, str) else json.dumps(claude_json)
                    )
            for rel, content in (repo_files or {}).items():
                path = os.path.join(repo, rel)
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "w") as f:
                    f.write(content)

            buf = io.StringIO()
            old_cwd = os.getcwd()
            os.chdir(repo)
            try:
                with (
                    patch.object(sys, "argv", ["toolrecall", "doctor", "claude", *args]),
                    patch.object(
                        shutil,
                        "which",
                        side_effect=lambda n: fake_claude if n == "claude" else None,
                    ),
                    patch.object(
                        subprocess,
                        "run",
                        return_value=subprocess.CompletedProcess([], 0, stdout="2.1.306\n"),
                    ),
                    patch.dict(os.environ, env or {}, clear=False),
                    _expand_to(home),
                    redirect_stdout(buf),
                ):
                    if not env:
                        os.environ.pop("ANTHROPIC_BASE_URL", None)
                    cli.cmd_doctor()
            finally:
                os.chdir(old_cwd)
            return buf.getvalue()

    def test_remote_http_only_is_zero_saving(self):
        out = self._run({"mcpServers": {"linear": {"type": "http", "url": "https://x"}}})
        self.assertIn("Remote HTTP:   1 server(s) [linear (user)]", out)
        self.assertIn("~0% token saving", out)
        self.assertNotIn("✅", out)

    def test_remote_sse_counts_as_remote(self):
        out = self._run({"mcpServers": {"s": {"type": "sse", "url": "https://x/sse"}}})
        self.assertIn("Remote HTTP:   1 server(s) [s (user)]", out)

    def test_user_scope_stdio_can_be_pooled(self):
        out = self._run({"mcpServers": {"fs": {"command": "npx"}}})
        self.assertIn("Local stdio:   1 server(s) [fs (user)]", out)
        self.assertIn("can be pooled", out)

    def test_local_scope_stdio_from_projects_entry(self):
        out = self._run(local_mcp={"pg": {"type": "stdio", "command": "uvx"}})
        self.assertIn("Local stdio:   1 server(s) [pg (local)]", out)

    def test_project_scope_mcp_json(self):
        out = self._run(
            {}, repo_files={".mcp.json": json.dumps({"mcpServers": {"db": {"command": "uvx"}}})}
        )
        self.assertIn("Local stdio:   1 server(s) [db (project)]", out)

    def test_mixed_remote_and_stdio(self):
        out = self._run({"mcpServers": {"linear": {"url": "https://x"}, "fs": {"command": "npx"}}})
        self.assertIn("Local stdio:   1 server(s) [fs (user)]", out)
        self.assertIn("Remote HTTP:   1 server(s) [linear (user)]", out)

    def test_malformed_claude_json_warns(self):
        out = self._run("{bad")
        self.assertIn("could not parse", out)

    def test_missing_claude_json(self):
        out = self._run()
        self.assertIn("Local stdio:   None", out)
        self.assertIn("~0% token saving", out)

    def test_full_bridge_registration_warns_in_verdict(self):
        out = self._run({"mcpServers": {"toolrecall": {"command": "toolrecall", "args": ["mcp"]}}})
        self.assertIn("registered as the full MCP bridge", out)
        self.assertIn("--multiplexer-only", out)

    def test_multiplexer_only_registration_no_warning(self):
        out = self._run(
            {
                "mcpServers": {
                    "toolrecall": {"command": "toolrecall", "args": ["mcp", "--multiplexer-only"]}
                }
            }
        )
        self.assertIn("multiplexer-only (safe)", out)
        self.assertNotIn("registered as the full MCP bridge", out)

    def test_commented_claude_p_is_not_a_hint(self):
        out = self._run({}, repo_files={"build.sh": '#!/bin/sh\n# tip: claude -p "hi"\necho ok\n'})
        self.assertNotIn("Hint:", out)
        self.assertNotIn("Headless repeat runs", out)

    def test_headless_hints_never_claim_savings(self):
        cases = {
            "print_flag": {".github/workflows/ci.yml": "steps:\n  - run: claude --print x\n"},
            "gh_action": {".github/workflows/c.yml": "- uses: anthropics/claude-code-action@v1\n"},
            "python_driver": {"eval.py": 'subprocess.run(["claude", "-p", task])\n'},
            "shell": {"loop.sh": 'claude -p "fix"\n'},
        }
        for name, files in cases.items():
            with self.subTest(name):
                out = self._run({}, repo_files=files)
                self.assertIn("Hint:", out)
                self.assertIn("doctor claude --headless", out)
                self.assertIn("~0% token saving", out)
                self.assertNotIn("Headless repeat runs:", out)

    def test_headless_flag_shows_measured_numbers(self):
        out = self._run({}, args=("--headless",))
        self.assertIn("Headless repeat runs (--headless)", out)
        # A rerun replays every call, so its cost is $0; the percentages are
        # cumulative over runs, never a per-rerun rate. The old mixed pair
        # (-77% per rerun / -72% after 4 runs) came from two different rounds.
        self.assertIn("$0 (every call replayed)", out)
        self.assertIn("−72% after 4", out)
        self.assertNotIn("−77%", out)
        self.assertIn("first run no saving", out)
        self.assertIn("claude-code-nogit", out)
        self.assertIn("ANTHROPIC_BASE_URL=http://127.0.0.1:8569", out)
        self.assertNotIn("96%", out)

    def test_headless_with_proxy_and_stdio_reports_both(self):
        out = self._run(
            {"mcpServers": {"fs": {"command": "npx"}}},
            args=("--headless",),
            env={"ANTHROPIC_BASE_URL": "http://127.0.0.1:8569"},
        )
        self.assertIn("Proxy Routing:    Active", out)
        self.assertIn("can be pooled", out)
        self.assertIn("Headless repeat runs:", out)
        self.assertNotIn("Route via", out)

    def test_interactive_flag_skips_scan(self):
        out = self._run({}, repo_files={"loop.sh": "claude -p x\n"}, args=("--interactive",))
        self.assertNotIn("Hint:", out)

    def test_audit_pointer_always_printed(self):
        self.assertIn("toolrecall audit claude-code --days 30", self._run({}))

    def test_no_claude_binary(self):
        import io
        from contextlib import redirect_stdout

        buf = io.StringIO()
        with patch.object(shutil, "which", return_value=None), redirect_stdout(buf):
            cli._diagnose_claude_code()
        self.assertIn("'claude' CLI binary not found", buf.getvalue())

    def test_scan_respects_time_budget(self):
        with tempfile.TemporaryDirectory() as repo:
            with open(os.path.join(repo, "loop.sh"), "w") as f:
                f.write("claude -p x\n")
            self.assertEqual(cli._scan_headless_hint(repo), ("loop.sh", 1))
            with patch.object(cli, "_DOCTOR_SCAN_MAX_SECONDS", -1):
                self.assertIsNone(cli._scan_headless_hint(repo))

    def test_scan_skips_hidden_dirs_but_not_github(self):
        with tempfile.TemporaryDirectory() as repo:
            os.makedirs(os.path.join(repo, ".cache"))
            with open(os.path.join(repo, ".cache", "x.sh"), "w") as f:
                f.write("claude -p x\n")
            self.assertIsNone(cli._scan_headless_hint(repo))
            os.makedirs(os.path.join(repo, ".github"))
            with open(os.path.join(repo, ".github", "ci.yml"), "w") as f:
                f.write("run: claude -p x\n")
            self.assertEqual(cli._scan_headless_hint(repo), (os.path.join(".github", "ci.yml"), 1))


if __name__ == "__main__":
    unittest.main()
