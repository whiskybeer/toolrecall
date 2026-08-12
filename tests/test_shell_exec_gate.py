"""
Security regression: cached_shell_exec must be gated identically to cached_terminal.

Issue: daemon._route routed cmd='cached_shell_exec' straight to
cache.cached_shell_exec(command) with NO SecurityGate.check_terminal call,
while 'cached_terminal' went through _handle_terminal which gates. This let
a client execute shell commands even when allow_terminal=false or when the
inner command failed the allowed_terminal_commands regex allowlist.

Fix: _route now sends cached_shell_exec through _handle_shell_exec, which
checks the wrapper-stripped inner command with SecurityGate.check_terminal
before executing.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from toolrecall.daemon import DaemonServer, SecurityGate


class MockConfig:
    """Minimal config object exposing the attributes SecurityGate reads."""

    def __init__(self, allow_terminal, allowed_paths=None, allowed_terminal_commands=None):
        self.mcp_allowed_paths = allowed_paths or []
        self.mcp_allow_terminal = allow_terminal
        self.mcp_allowed_terminal_commands = allowed_terminal_commands or []
        self.mcp_allow_invalidate = False
        self.mcp_multiplex_enabled = False
        self.mcp_multiplex_servers = []
        self.mcp_tool_access_control = False
        self.mcp_dangerous_tool_keywords = []
        self.mcp_cognitive_check_enabled = False
        self.mcp_ast_check_enabled = False


def make_server(allow_terminal, allowlist=None):
    """A DaemonServer with a real SecurityGate but no socket — enough to test _route."""
    server = DaemonServer.__new__(DaemonServer)
    server.security = SecurityGate(MockConfig(allow_terminal, allowed_terminal_commands=allowlist))
    return server


class TestShellExecGate(unittest.TestCase):
    def test_blocked_when_terminal_disabled(self):
        """cached_shell_exec must be rejected when allow_terminal=false."""
        server = make_server(allow_terminal=False)
        for cmd in ("ls", "git status", "rm -rf /", "cat /etc/shadow"):
            with self.subTest(cmd=cmd):
                res = server._route({"cmd": "cached_shell_exec", "command": cmd})
                self.assertIn("error", res, f"cmd={cmd} should be gated when allow_terminal=false")

    def test_blocked_when_not_in_allowlist(self):
        """A destructive command failing the regex allowlist is rejected."""
        server = make_server(allow_terminal=True, allowlist=["^ls(\\s+|$)", "^cat(\\s+|$)"])
        res = server._route({"cmd": "cached_shell_exec", "command": "rm -rf /"})
        self.assertIn("error", res, "Destructive command not in allowlist must be blocked")

    def test_allowed_when_matches_allowlist(self):
        """A read-only command matching the allowlist passes the gate and executes."""
        server = make_server(allow_terminal=True, allowlist=["^ls(\\s+|$)", "^cat(\\s+|$)"])
        res = server._route({"cmd": "cached_shell_exec", "command": "ls -la /tmp"})
        self.assertNotIn("error", res)
        self.assertIn("exit_code", res)

    def test_wrapper_stripped_before_gate(self):
        """A wrapped bash -c command is gated on its inner command."""
        server = make_server(allow_terminal=True, allowlist=["^ls(\\s+|$)"])
        # 'rm' is not in the allowlist — even nested inside a wrapper it must be blocked
        wrapped = "bash -c 'source ~/.profile; eval '\"'\"'rm -rf /'\"'\"''"
        res = server._route({"cmd": "cached_shell_exec", "command": wrapped})
        self.assertIn("error", res, "Wrapped destructive command must be gated")


if __name__ == "__main__":
    unittest.main()
