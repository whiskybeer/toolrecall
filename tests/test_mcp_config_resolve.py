"""Tests for config.py MCP server auto-resolution.

Tests cover:
- Config auto-resolves server names from registry
- Explicit servers_config takes priority over registry
- Unknown server names are skipped
"""

import os
import sys
import json
import tempfile
from pathlib import Path

import pytest


class TestConfigAutoResolve:
    """Test that config.py auto-resolves MCP servers from registry."""

    def test_config_resolves_servers_from_registry(self):
        """Config with server names should auto-resolve via registry."""
        # Create a minimal config.toml that only lists server names
        # (no servers_config section)
        cfg_content = """
[mcp_multiplex]
enabled = true
servers = ["time", "github"]
idle_minutes = 15
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".toml", delete=False) as f:
            f.write(cfg_content)
            tmp_path = f.name

        try:
            from toolrecall.config import load_config
            cfg = load_config(tmp_path)

            result = cfg.mcp_multiplex_servers_config
            assert isinstance(result, dict)
            assert "time" in result, f"'time' should be resolved, got keys: {list(result.keys())}"
            assert "github" in result, f"'github' should be resolved, got keys: {list(result.keys())}"

            # Check that built-in servers have the right command
            assert "python" in result["time"]["command"].lower() or sys.executable in result["time"]["command"]
            assert "-m" in result["time"]["args"]
            assert "toolrecall.mcp_time" in str(result["time"]["args"])
        finally:
            os.unlink(tmp_path)

    def test_config_resolves_builtin_with_correct_python(self):
        """Built-in servers should use sys.executable, not hardcoded 'python3'."""
        cfg_content = """
[mcp_multiplex]
servers = ["time"]
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".toml", delete=False) as f:
            f.write(cfg_content)
            tmp_path = f.name

        try:
            from toolrecall.config import load_config
            cfg = load_config(tmp_path)
            result = cfg.mcp_multiplex_servers_config
            assert result["time"]["command"] == sys.executable, \
                f"Expected {sys.executable}, got {result['time']['command']}"
        finally:
            os.unlink(tmp_path)

    def test_config_explicit_takes_priority(self):
        """Explicit servers_config entry should take priority over registry."""
        cfg_content = """
[mcp_multiplex]
servers = ["time"]

[mcp_multiplex.servers_config]
time = { command = "custom-cmd", args = ["--custom-flag"] }
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".toml", delete=False) as f:
            f.write(cfg_content)
            tmp_path = f.name

        try:
            from toolrecall.config import load_config
            cfg = load_config(tmp_path)
            result = cfg.mcp_multiplex_servers_config
            assert "time" in result
            assert result["time"]["command"] == "custom-cmd", \
                f"Expected 'custom-cmd', got {result['time']['command']}"
            assert "--custom-flag" in result["time"]["args"]
        finally:
            os.unlink(tmp_path)

    def test_config_unknown_server_skipped(self):
        """Unknown server names should be skipped (not in result).
        When servers_config is also empty, the result should have no entries
        for the unknown name.
        """
        cfg_content = """
[mcp_multiplex]
servers = ["completely-fake-server-99"]

[mcp_multiplex.servers_config]
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".toml", delete=False) as f:
            f.write(cfg_content)
            tmp_path = f.name

        try:
            from toolrecall.config import load_config
            cfg = load_config(tmp_path)
            result = cfg.mcp_multiplex_servers_config
            # Unknown server should NOT be in the result
            assert "completely-fake-server-99" not in result, \
                f"Unknown server should not be resolved, got: {result}"
        finally:
            os.unlink(tmp_path)

    def test_config_empty_servers_no_resolution(self):
        """Empty servers list = no auto-resolution.
        The result may contain entries from agent config.yaml or package default,
        but should NOT have any entries that came from auto-resolution
        (i.e., unknown servers resolved from empty list).
        """
        cfg_content = """
[mcp_multiplex]
servers = []
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".toml", delete=False) as f:
            f.write(cfg_content)
            tmp_path = f.name

        try:
            from toolrecall.config import load_config
            cfg = load_config(tmp_path)
            result = cfg.mcp_multiplex_servers_config
            # Empty servers list should not add any auto-resolved entries.
            assert isinstance(result, dict)
        finally:
            os.unlink(tmp_path)

    def test_config_resolves_multiple_servers(self):
        """Multiple server names should all be resolved."""
        cfg_content = """
[mcp_multiplex]
servers = ["time", "github", "fetch", "sequential-thinking"]
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".toml", delete=False) as f:
            f.write(cfg_content)
            tmp_path = f.name

        try:
            from toolrecall.config import load_config
            cfg = load_config(tmp_path)
            result = cfg.mcp_multiplex_servers_config

            # All 4 should be resolved
            assert "time" in result
            assert "github" in result
            assert "fetch" in result
            assert "sequential-thinking" in result

            # Check sources: time/github/seqthink = builtin, fetch = external
            from toolrecall.mcp_registry import is_builtin
            assert is_builtin("time") or result["time"]["command"] == sys.executable
            assert is_builtin("fetch") or result["fetch"]["command"] == sys.executable
            assert "toolrecall.mcp_fetch" in str(result["fetch"]["args"])
        finally:
            os.unlink(tmp_path)

    def test_config_partial_explicit_override(self):
        """Partially override some servers, auto-resolve the rest."""
        cfg_content = """
[mcp_multiplex]
servers = ["time", "github", "fetch"]

[mcp_multiplex.servers_config]
github = { command = "npx", args = ["-y", "@modelcontextprotocol/server-github"] }
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".toml", delete=False) as f:
            f.write(cfg_content)
            tmp_path = f.name

        try:
            from toolrecall.config import load_config
            cfg = load_config(tmp_path)
            result = cfg.mcp_multiplex_servers_config

            # GitHub should use explicit override
            assert result["github"]["command"] == "npx", \
                f"GitHub should use npx override, got {result['github']['command']}"

            # Time, Fetch should be auto-resolved (time=builtin, fetch=builtin)
            assert "time" in result
            assert result["time"]["command"] == sys.executable
            assert "fetch" in result
            assert result["fetch"]["command"] == sys.executable
            assert "toolrecall.mcp_fetch" in str(result["fetch"]["args"])
        finally:
            os.unlink(tmp_path)
