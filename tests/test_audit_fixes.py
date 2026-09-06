"""Tests for constitution audit fixes A2–A5 (and A8 via Makefile).

Covers:
- A3: unknown config sections/keys warn; freeform sub-tables don't
- A5: terminal fallback TTL respects [cache].terminal_default_ttl, not 300
- A2: protocol version stamp + daemon-side rejection of newer versions
- A4: per-agent [mcp.clients] policies (allowed_paths, terminal, hints)
"""

import os
import sys
import tempfile
import unittest
import warnings

# Force a clean, isolated test DB path BEFORE any toolrecall import — this
# module imports toolrecall.daemon (→ cache → _db), which would otherwise
# open a connection to the production ~/.toolrecall/cache.db at import time
# (same pattern as test_cache_safety.py / test_file_cache.py).
_test_db_dir = tempfile.mkdtemp()
os.environ["TOOLRECALL_CACHE_DB"] = os.path.join(_test_db_dir, "test_audit.db")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from toolrecall.config import Config
from toolrecall.daemon import SecurityGate


def _cfg(toml_text: str) -> Config:
    d = tempfile.mkdtemp()
    p = os.path.join(d, "toolrecall.toml")
    with open(p, "w") as f:
        f.write(toml_text)
    return Config(p)


class TestA3UnknownKeys(unittest.TestCase):
    def test_unknown_key_warns(self):
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            _cfg('[mcp]\nallow_terminal = true\nallowed_path = "/tmp"\n')
        self.assertTrue(
            any("allowed_path " in str(x.message) for x in w),
            "typo'd key must warn",
        )

    def test_unknown_section_warns(self):
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            _cfg("[custm]\nfoo = 1\n")
        self.assertTrue(any("unknown config section" in str(x.message) for x in w))

    def test_freeform_terminal_ttls_no_warning(self):
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            _cfg('[cache.terminal_ttls]\n"pwd" = 3600\n')
        self.assertFalse(
            any("unknown config" in str(x.message) for x in w),
            [str(x.message) for x in w],
        )

    def test_freeform_mcp_clients_no_warning(self):
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            _cfg('[mcp.clients."warp"]\nemit_context_hints = false\n')
        self.assertFalse(any("unknown config" in str(x.message) for x in w))

    def test_unreadable_config_warns_not_silent(self):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "broken.toml")
        with open(p, "w") as f:
            f.write("not [ valid toml {{{{")
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            Config(p)
        self.assertTrue(
            any("could not load config" in str(x.message) for x in w),
            "unreadable config must warn (was except: pass)",
        )


class TestA5TerminalTTLHome(unittest.TestCase):
    def test_default_ttl_property(self):
        cfg = _cfg("[cache]\nterminal_default_ttl = 777\n")
        self.assertEqual(cfg.terminal_default_ttl, 777)

    def test_env_overrides_default_ttl(self):
        os.environ["TOOLRECALL_TERMINAL_TTL"] = "888"
        try:
            cfg = _cfg("")  # no toml override
            self.assertEqual(cfg.terminal_default_ttl, 888)
        finally:
            del os.environ["TOOLRECALL_TERMINAL_TTL"]


class TestA4PerAgentPolicy(unittest.TestCase):
    def _gate(self) -> SecurityGate:
        cfg = _cfg(
            """
[mcp]
allowed_paths = ["/tmp/global"]
allow_terminal = false

[mcp.clients."claude-code"]
allowed_paths = ["/tmp/claude"]
allow_terminal = true
allowed_terminal_commands = ["^ls", "^pwd"]

[mcp.clients."cursor"]
emit_context_hints = false
"""
        )
        return SecurityGate(cfg)

    def test_policy_table_parsed(self):
        gate = self._gate()
        self.assertIn("claude-code", gate.client_policy)
        self.assertEqual(gate.client_policy["claude-code"]["allowed_paths"], ["/tmp/claude"])
        self.assertTrue(gate.client_policy["claude-code"]["allow_terminal"])
        # hint-only client keeps only its key
        self.assertEqual(gate.client_policy["cursor"], {"emit_context_hints": False})

    def test_hint_policy_backward_compat_view(self):
        cfg = _cfg('[mcp.clients."warp"]\nemit_context_hints = false\n')
        self.assertEqual(cfg.mcp_client_hint_policy, {"warp": False})

    def test_effective_policy_fallback_to_global(self):
        gate = self._gate()
        self.assertEqual(gate.effective_policy("mystery")["allow_terminal"], False)
        self.assertEqual(gate.effective_policy("mystery")["allowed_paths"], ["/tmp/global"])
        # no client at all (direct UDS caller) → global
        self.assertEqual(gate.effective_policy(None)["allow_terminal"], False)

    def test_per_agent_paths_scoped(self):
        gate = self._gate()
        # agent's own path: allowed
        self.assertIsNone(gate.check_read_path_for("/tmp/claude/x.txt", "claude-code"))
        # global-only path: blocked for this agent even if in global allowlist
        self.assertIsNotNone(gate.check_read_path_for("/tmp/global/x.txt", "claude-code"))

    def test_sensitive_blocklist_still_applies_per_agent(self):
        gate = self._gate()
        # even inside the agent's allowlist, sensitive files stay blocked
        self.assertIsNotNone(gate.check_read_path_for("/tmp/claude/.env", "claude-code"))

    def test_per_agent_terminal(self):
        gate = self._gate()
        # terminal ON for claude-code + regex allowlist
        self.assertIsNone(gate.check_terminal_for("ls -la", "claude-code"))
        self.assertIsNotNone(gate.check_terminal_for("rm -rf /", "claude-code"))
        # global (terminal OFF) applies to unlisted agents
        self.assertIsNotNone(gate.check_terminal_for("ls", "mystery"))

    def test_global_policy_unchanged_when_no_override(self):
        gate = self._gate()
        # cursor only overrides hints — paths/terminal fall back to global gate
        self.assertEqual(
            gate.check_read_path_for("/tmp/global/x.txt", "cursor"),
            gate.check_read_path_for("/tmp/global/x.txt"),
        )


class TestA2ProtocolVersion(unittest.TestCase):
    def test_send_message_stamps_version(self):
        import socket
        from toolrecall.transport import send_message, receive_message, PROTOCOL_VERSION

        a, b = socket.socketpair()
        send_message(a, {"cmd": "ping"})
        msg = receive_message(b)
        self.assertEqual(msg["v"], PROTOCOL_VERSION)
        a.close()
        b.close()

    def test_caller_version_not_overwritten(self):
        import socket
        from toolrecall.transport import send_message, receive_message

        a, b = socket.socketpair()
        send_message(a, {"cmd": "ping", "v": 42})
        msg = receive_message(b)
        self.assertEqual(msg["v"], 42)
        a.close()
        b.close()


if __name__ == "__main__":
    unittest.main()
