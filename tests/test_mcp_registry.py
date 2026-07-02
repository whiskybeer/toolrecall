"""Tests for MCP Server Registry.

Tests cover:
- Built-in server resolution (time, seqthink, github)
- External server resolution (fetch, filesystem, etc.)
- Case-insensitive lookup
- Unknown server returns None
- registry listing
- uvx detection
"""

import os
import sys
import json
import tempfile
from pathlib import Path

import pytest

from toolrecall.mcp_registry import (
    resolve_server,
    is_builtin,
    is_known,
    list_registered_servers,
    has_uvx,
    BUILTIN_SERVERS,
    EXTERNAL_REGISTRY,
)


class TestMCPRegistry:
    """Test the MCP server registry module."""

    def test_resolve_builtin_time(self):
        """Time is a built-in server."""
        result = resolve_server("time")
        assert result is not None
        cmd, args, source = result
        assert source == "builtin"
        assert "python" in cmd.lower() or sys.executable in cmd
        assert "-m" in args
        assert "toolrecall.mcp_time" in args

    def test_resolve_builtin_sequential_thinking(self):
        """Sequential-thinking is built-in."""
        result = resolve_server("sequential-thinking")
        assert result is not None
        _, _, source = result
        assert source == "builtin"

    def test_resolve_builtin_github(self):
        """GitHub is built-in (stdlib replacement for npm package)."""
        result = resolve_server("github")
        assert result is not None
        _, _, source = result
        assert source == "builtin"

    def test_resolve_builtin_fetch(self):
        """Fetch is now a built-in server (stdlib replacement for uvx)."""
        result = resolve_server("fetch")
        assert result is not None
        _, _, source = result
        assert source == "builtin"

    def test_resolve_external_filesystem(self):
        """Filesystem is an external (uvx-based) server."""
        result = resolve_server("filesystem")
        assert result is not None
        _, _, source = result
        assert source == "external"

    def test_resolve_case_insensitive(self):
        """Lookup is case-insensitive."""
        assert resolve_server("TIME") == resolve_server("time")
        assert resolve_server("Fetch") == resolve_server("fetch")
        assert resolve_server("SEQUENTIAL-THINKING") == resolve_server("sequential-thinking")

    def test_resolve_unknown_returns_none(self):
        """Unknown server name returns None."""
        assert resolve_server("nonexistent-server-12345") is None
        assert resolve_server("") is None

    def test_resolve_all_builtins_known(self):
        """Every built-in server is resolvable."""
        for name in BUILTIN_SERVERS:
            assert is_known(name), f"Built-in '{name}' should be known"

    def test_resolve_all_externals_known(self):
        """Every external server is resolvable."""
        for name in EXTERNAL_REGISTRY:
            assert is_known(name), f"External '{name}' should be known"
            result = resolve_server(name)
            assert result is not None
            assert result[2] == "external"

    def test_is_builtin(self):
        """is_builtin returns True only for built-in servers."""
        for name in BUILTIN_SERVERS:
            assert is_builtin(name), f"'{name}' should be builtin"
        assert not is_builtin("filesystem"), "filesystem is external, not builtin"
        assert not is_builtin("nonexistent"), "unknown is not builtin"

    def test_is_known(self):
        """is_known returns True for registered servers."""
        assert is_known("time")
        assert is_known("fetch")
        assert not is_known("imaginary-server-v99")

    def test_list_registered_servers(self):
        """list_registered_servers returns all servers with metadata."""
        servers = list_registered_servers()
        assert isinstance(servers, list)
        assert len(servers) >= 3  # At least our 3 built-ins

        # Check structure
        for srv in servers:
            assert "name" in srv
            assert "source" in srv
            assert "command" in srv
            assert "args" in srv
            assert srv["source"] in ("builtin", "external")

        # Check built-ins are in the list
        names = [s["name"] for s in servers]
        assert "time" in names
        assert "sequential-thinking" in names
        assert "github" in names
        assert "fetch" in names

    def test_has_uvx(self):
        """has_uvx checks PATH for uvx binary."""
        # Just verify it runs without error
        result = has_uvx()
        assert isinstance(result, bool)

    def test_resolve_returns_copy_of_args(self):
        """resolve_server returns a fresh copy of args list."""
        r1 = resolve_server("time")
        r2 = resolve_server("time")
        assert r1 is not None and r2 is not None
        # Modify one — shouldn't affect the other
        r1[1].append("--extra")
        assert "--extra" not in r2[1]

    def test_all_external_uvx_default(self):
        """All external servers default to uvx command."""
        for name in EXTERNAL_REGISTRY:
            result = resolve_server(name)
            assert result is not None
            cmd, args, source = result
            assert source == "external"
            assert cmd == "uvx", f"'{name}' should use uvx, got '{cmd}'"
            assert len(args) >= 1

    def test_builtin_python_command(self):
        """Built-in servers use the current python executable."""
        for name in BUILTIN_SERVERS:
            result = resolve_server(name)
            assert result is not None
            cmd, args, source = result
            assert source == "builtin"
            assert cmd.lower() == "python3" or cmd == sys.executable
