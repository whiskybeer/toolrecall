"""Tests for optional Python MCP server config parsing in toolrecall.toml.

Tests validate the TOML configuration directly — the Config class has
read-only properties that are managed by the daemon lifecycle.
We test: TOML parse succeeds, server definitions are correct,
the real config file loads without errors.
"""

import json
import os
import sys
import unittest
import tempfile
import shutil
import tomllib

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _parse_toml(text: str) -> dict:
    """Parse TOML string and return the servers_config dict."""
    data = tomllib.loads(text)
    mux = data.get("mcp_multiplex", {})
    return mux.get("servers_config", {})


class TestMCPConfig(unittest.TestCase):
    """Test that TOML config correctly defines Python MCP server entries."""

    def test_python_github_server_config(self):
        """Prove python://github is correctly configured in TOML."""
        servers = _parse_toml("""
[mcp_multipplex]
[mcp_multiplex]
servers_config.github = { command = "python3", args = ["-m", "toolrecall.mcp_github"] }
""")
        gh = servers.get("github", {})
        self.assertEqual(gh["command"], "python3")
        self.assertEqual(gh["args"], ["-m", "toolrecall.mcp_github"])

    def test_python_time_server_config(self):
        """Prove python://time is correctly configured in TOML."""
        servers = _parse_toml("""
[mcp_multiplex]
servers_config.time = { command = "python3", args = ["-m", "toolrecall.mcp_time"] }
""")
        t = servers.get("time", {})
        self.assertEqual(t["command"], "python3")
        self.assertEqual(t["args"], ["-m", "toolrecall.mcp_time"])

    def test_python_seqthink_server_config(self):
        """Prove python://seqthink is correctly configured in TOML."""
        servers = _parse_toml("""
[mcp_multiplex]
servers_config.seqthink = { command = "python3", args = ["-m", "toolrecall.mcp_seqthink"] }
""")
        st = servers.get("seqthink", {})
        self.assertEqual(st["command"], "python3")
        self.assertEqual(st["args"], ["-m", "toolrecall.mcp_seqthink"])

    def test_all_three_python_servers(self):
        """Prove all 3 Python MCP servers can be configured simultaneously."""
        servers = _parse_toml("""
[mcp_multiplex]
servers_config.github = { command = "python3", args = ["-m", "toolrecall.mcp_github"] }
servers_config.time = { command = "python3", args = ["-m", "toolrecall.mcp_time"] }
servers_config.seqthink = { command = "python3", args = ["-m", "toolrecall.mcp_seqthink"] }
""")
        self.assertIn("github", servers)
        self.assertIn("time", servers)
        self.assertIn("seqthink", servers)
        self.assertEqual(len(servers), 3)

    def test_mixed_npx_and_python_servers(self):
        """Prove Python and npx servers can coexist in config."""
        servers = _parse_toml("""
[mcp_multiplex]
servers_config.python_github = { command = "python3", args = ["-m", "toolrecall.mcp_github"] }
servers_config.npx_time = { command = "npx", args = ["-y", "@modelcontextprotocol/server-time"] }
""")
        self.assertEqual(servers["python_github"]["command"], "python3")
        self.assertEqual(servers["npx_time"]["command"], "npx")

    def test_real_config_file_parses(self):
        """Prove the actual toolrecall.toml parses without errors."""
        actual_path = os.path.join(
            os.path.dirname(__file__), "..", "toolrecall", "config.toml"
        )
        self.assertTrue(os.path.exists(actual_path))
        with open(actual_path, "rb") as f:
            data = tomllib.load(f)
        # At minimum, mcp_multiplex section exists
        self.assertIn("mcp_multiplex", data)


if __name__ == "__main__":
    unittest.main()
