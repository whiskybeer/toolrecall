"""The server allowlist must not block the multiplexer's discovery paths.

`check_mcp_server("_any")` is the multiplexer-wide sentinel used by
`mcp_list_servers` and `mcp_tools_catalog`. Both enumerate the allowlist
themselves, so the allowlist must not reject the sentinel.

Live bug (found 2026-09-28 on the running daemon): with
`servers = ["fetch", "hermes-docs", "sqlite-browser"]`, `mcp_list_servers`
answered `{"error": "MCP server '_any' not in allowed_servers."}` — every
discovery path was dead for anyone who configured an allowlist, and no test
covered the gate.
"""

from __future__ import annotations

import unittest


class _FakeCfg:
    """Just enough config surface for SecurityGate.__init__."""

    def __init__(self, servers=None, multiplex=True):
        self.mcp_allowed_paths = []
        self.mcp_allow_terminal = False
        self.mcp_allowed_terminal_commands = []
        self.mcp_allow_invalidate = False
        self.mcp_multiplex_enabled = multiplex
        self.mcp_multiplex_servers = servers if servers is not None else []
        self.mcp_tool_access_control = False
        self.mcp_dangerous_tool_keywords = []
        self.mcp_allow_symlink_escapes = False
        self.mcp_cognitive_check_enabled = False
        self.mcp_ast_check_enabled = False

    def mcp_client_policy(self):
        return {}


def _gate(servers=None, multiplex=True):
    from toolrecall.daemon import SecurityGate

    return SecurityGate(_FakeCfg(servers, multiplex))


class TestDiscoverySentinel(unittest.TestCase):
    def test_any_sentinel_passes_with_a_non_empty_allowlist(self):
        gate = _gate(["fetch", "hermes-docs"])
        self.assertIsNone(gate.check_mcp_server("_any"))

    def test_any_sentinel_passes_without_an_allowlist(self):
        self.assertIsNone(_gate([]).check_mcp_server("_any"))

    def test_allowlisted_server_passes(self):
        self.assertIsNone(_gate(["fetch", "hermes-docs"]).check_mcp_server("fetch"))

    def test_unlisted_server_is_still_blocked(self):
        """The fix must not turn the allowlist into a no-op."""
        err = _gate(["fetch"]).check_mcp_server("github")
        self.assertIsNotNone(err)
        self.assertIn("allowed_servers", err)

    def test_disabled_multiplexer_blocks_the_sentinel_too(self):
        """Discovery is part of the multiplexer: disabling it disables discovery."""
        err = _gate(["fetch"], multiplex=False).check_mcp_server("_any")
        self.assertIsNotNone(err)
        self.assertIn("disabled", err)

    def test_case_insensitive_allowlist(self):
        self.assertIsNone(_gate(["fetch"]).check_mcp_server("FETCH"))


if __name__ == "__main__":
    unittest.main()
