"""Freeform entry-field validation (2026-09 review follow-up).

`_warn_unknown_keys` used to validate freeform sub-table ENTRY fields
against the PARENT section's key list. That false-positived fields the
daemon/multiplexer actually read — `[network.clients."id"] allowed_paths`
and `private_writes` ("unknown key ... (key ignored)") and
`[mcp_multiplex.servers_config."srv"] command/args` — while telling the
user the key was IGNORED when it is honored. Worse, the old code only
inspected the FIRST entry, so a typo'd field in a later client never
warned.

Now: per-freeform entry schemas in `Config._FREEFORM_ENTRY_KEYS`, every
entry validated, real typos still caught. This file pins that contract.
"""

import os
import sys
import tempfile
import unittest
import warnings

_test_db_dir = tempfile.mkdtemp()
os.environ["TOOLRECALL_CACHE_DB"] = os.path.join(_test_db_dir, "test_freeform.db")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from toolrecall.config import Config


def _cfg(toml_text: str):
    d = tempfile.mkdtemp()
    p = os.path.join(d, "toolrecall.toml")
    with open(p, "w") as f:
        f.write(toml_text)
    return p


def _warnings_for(toml_text: str) -> list[str]:
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        Config(_cfg(toml_text))
    return [str(x.message) for x in w]


class TestNetworkClientsEntryFields(unittest.TestCase):
    """[network.clients."id"] fields are READ by the daemon (policy, ns)."""

    def test_real_fields_no_false_typo_warning(self):
        warns = _warnings_for(
            '[network.clients."alice"]\n'
            'token = "t"\n'
            'allowed_paths = ["/srv"]\n'
            "allow_terminal = false\n"
            'allowed_terminal_commands = ["^git "] \n'
            "private_writes = true\n"
        )
        self.assertFalse(any("unknown key" in m for m in warns), warns)

    def test_typo_in_entry_field_still_warns(self):
        warns = _warnings_for('[network.clients."a"]\nallowed_path = ["/x"]\n')
        self.assertTrue(any("[network.clients.*] allowed_path" in m for m in warns), warns)

    def test_typo_in_SECOND_entry_caught(self):
        """The old code validated only the FIRST entry — regression pin."""
        warns = _warnings_for(
            '[network.clients."a"]\ntoken = "t"\n\n[network.clients."b"]\ntokens = "typo"\n'
        )
        self.assertTrue(any("[network.clients.*] tokens" in m for m in warns), warns)


class TestServersConfigEntryFields(unittest.TestCase):
    """[mcp_multiplex.servers_config."srv"] fields are read verbatim."""

    def test_command_args_env_ttl_no_warning(self):
        warns = _warnings_for(
            '[mcp_multiplex.servers_config."fetch"]\n'
            'command = "uvx"\n'
            'args = ["mcp-server-fetch"]\n'
            'env = { "KEY" = "v" }\n'
            "ttl = 120\n"
        )
        self.assertFalse(any("unknown key" in m for m in warns), warns)

    def test_typo_in_server_field_warns(self):
        warns = _warnings_for('[mcp_multiplex.servers_config."fetch"]\ncomand = "uvx"\n')
        self.assertTrue(any("[mcp_multiplex.servers_config.*] comand" in m for m in warns), warns)


class TestMcpClientsEntryFields(unittest.TestCase):
    def test_policy_fields_no_warning(self):
        warns = _warnings_for(
            '[mcp.clients."warp"]\nemit_context_hints = false\n'
            'allowed_paths = ["/tmp"]\nallow_terminal = false\n'
            'allowed_terminal_commands = ["^ls"]\n'
        )
        self.assertFalse(any("unknown key" in m for m in warns), warns)

    def test_typo_in_policy_field_warns(self):
        warns = _warnings_for('[mcp.clients."warp"]\nallowed_path = ["/tmp"]\n')
        self.assertTrue(any("[mcp.clients.*] allowed_path" in m for m in warns), warns)


class TestSchemalessFreeformUntouched(unittest.TestCase):
    """terminal_ttls (scalar values) and sources.memory (user-defined store
    names) have no entry schema — no entry-level warnings, still no unknown-
    section/unknown-key noise."""

    def test_terminal_ttls_no_warning(self):
        warns = _warnings_for('[cache.terminal_ttls]\n"pwd" = 3600\n')
        self.assertFalse(any("unknown" in m for m in warns), warns)

    def test_sources_memory_no_warning(self):
        warns = _warnings_for('[sources]\nmemory = { "proj" = { path = "/tmp" } }\n')
        self.assertFalse(any("unknown" in m for m in warns), warns)


if __name__ == "__main__":
    unittest.main()
