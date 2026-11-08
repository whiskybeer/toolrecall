# Copyright (c) 2026 Robin Schultka
# SPDX-License-Identifier: MIT
# Source: https://github.com/whiskybeer/toolrecall
"""MCP multiplexer: two live-found bugs, fixed 2026-09-22.

Bug A — error responses were cached. A failed tool call (network blip,
crashed subprocess, permission error) was stored like an answer and then
*replayed* to every identical call for the whole TTL, converting a
retryable miss into a dead key. Found live: a daemon-env fetch failure
("Permission denied") was stored and served from cache on call 2.

Bug B — a partial servers_config override erased the registry command.
`fetch = { ttl = 300 }` merged to command="" → subprocess.Popen("") →
"Permission denied: ''" on every call. Overrides must merge with the
registry entry, not replace it.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

from toolrecall import config as config_mod
from toolrecall import daemon as daemon_mod


class _Unit:
    """Expose the daemon's static error classifier without a DaemonServer."""

    _mcp_result_is_error = staticmethod(daemon_mod.DaemonServer._mcp_result_is_error)


class TestErrorDetection(unittest.TestCase):
    def test_dict_with_error_key_is_error(self) -> None:
        assert _Unit._mcp_result_is_error({"error": "boom"}) is True

    def test_json_encoded_error_is_error(self) -> None:
        # stdlib servers return results as JSON strings
        assert _Unit._mcp_result_is_error(json.dumps({"error": "fetch failed"})) is True

    def test_success_dict_is_not_error(self) -> None:
        assert _Unit._mcp_result_is_error({"status": 200, "body": "ok"}) is False

    def test_error_mentioned_in_body_is_content(self) -> None:
        """A page quoting an error message is a successful fetch — its text
        may legitimately contain the word error at any nesting level."""
        body = json.dumps({"status": 200, "text": "the error log says: boom"})
        assert _Unit._mcp_result_is_error(body) is False

    def test_plain_string_is_content(self) -> None:
        assert _Unit._mcp_result_is_error("plain text body") is False

    def test_non_json_string_is_content(self) -> None:
        assert _Unit._mcp_result_is_error("{not json") is False


class TestDaemonNoErrorStore(unittest.TestCase):
    """_handle_mcp_call must not store failures — and must heal poisoned rows."""

    def _daemon_with_multiplexer(self, result: object, tmp_dir: str):
        d = daemon_mod.DaemonServer(socket_path=os.path.join(tmp_dir, "t.sock"))
        d._server = None
        d.multiplexer = mock.Mock()
        d.multiplexer.cfg = mock.Mock()
        d.multiplexer.cfg.mcp_multiplex_transparent_cache = True
        d.multiplexer.cfg.mcp_multiplex_default_ttl = 60
        d.multiplexer.cfg.mcp_multiplex_servers_config = {}
        d.multiplexer.cfg.mcp_multiplex_fetch_revalidate = False
        d.multiplexer.call.return_value = result
        d.multiplexer.cfg.mcp_multiplex_servers_config = {"fetch": {"ttl": 60}}
        return d

    def test_error_result_is_not_stored(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            d = self._daemon_with_multiplexer({"error": "fetch: boom"}, td)
            with (
                mock.patch.object(daemon_mod, "_cache_mcp_store") as store,
                mock.patch.object(
                    daemon_mod, "_cache_mcp_check", mock.Mock(return_value={"key": "k1"})
                ),
                mock.patch.object(daemon_mod, "_heal_poisoned_mcp_row"),
            ):
                d._handle_mcp_call(
                    {"cmd": "mcp_call", "server": "fetch", "tool": "fetch_url", "arguments": {}}
                )
            store.assert_not_called()

    def test_error_result_heals_a_poisoned_row(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            d = self._daemon_with_multiplexer({"error": "fetch: boom"}, td)
            with (
                mock.patch.object(daemon_mod, "_cache_mcp_store") as store,
                mock.patch.object(
                    daemon_mod, "_cache_mcp_check", mock.Mock(return_value={"key": "k1"})
                ),
                mock.patch.object(daemon_mod, "_heal_poisoned_mcp_row") as heal,
            ):
                d._handle_mcp_call(
                    {"cmd": "mcp_call", "server": "fetch", "tool": "fetch_url", "arguments": {}}
                )
            store.assert_not_called()
            heal.assert_called_once_with("k1")

    def test_success_result_is_stored(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            d = self._daemon_with_multiplexer({"status": 200, "body": "ok"}, td)
            with (
                mock.patch.object(daemon_mod, "_cache_mcp_store") as store,
                mock.patch.object(
                    daemon_mod, "_cache_mcp_check", mock.Mock(return_value={"key": "k2"})
                ),
                mock.patch.object(daemon_mod, "_heal_poisoned_mcp_row"),
            ):
                d._handle_mcp_call(
                    {"cmd": "mcp_call", "server": "fetch", "tool": "fetch_url", "arguments": {}}
                )
            store.assert_called_once()

    def test_json_string_error_is_not_stored(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            d = self._daemon_with_multiplexer(json.dumps({"error": "fetch: start failed"}), td)
            with (
                mock.patch.object(daemon_mod, "_cache_mcp_store") as store,
                mock.patch.object(
                    daemon_mod, "_cache_mcp_check", mock.Mock(return_value={"key": "k3"})
                ),
                mock.patch.object(daemon_mod, "_heal_poisoned_mcp_row"),
            ):
                d._handle_mcp_call(
                    {"cmd": "mcp_call", "server": "fetch", "tool": "fetch_url", "arguments": {}}
                )
            store.assert_not_called()


class TestPartialOverrideMerge(unittest.TestCase):
    """A ttl-only override must keep the registry command — not blank it."""

    def _cfg(self) -> config_mod.Config:
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "toolrecall.toml")
            with open(p, "w") as f:
                f.write(
                    "[mcp_multiplex]\nenabled = true\n"
                    'servers = ["fetch"]\n'
                    "[mcp_multiplex.servers_config]\n"
                    "fetch = { ttl = 300 }\n"
                )
            return config_mod.Config(p)

    def test_ttl_only_override_keeps_registry_command(self) -> None:
        cfg = self._cfg()
        resolved = cfg.mcp_multiplex_servers_config.get("fetch") or {}
        self.assertEqual(resolved.get("command"), sys.executable)
        self.assertEqual(resolved.get("args"), ["-m", "toolrecall.mcp_fetch"])
        self.assertEqual(resolved.get("ttl"), 300)

    def test_full_override_still_wins(self) -> None:
        """A user-supplied command is never overwritten by the registry."""
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "toolrecall.toml")
            with open(p, "w") as f:
                f.write(
                    "[mcp_multiplex]\nservers = ['fetch']\n"
                    "[mcp_multiplex.servers_config]\n"
                    "fetch = { command = '/custom/fetch', args = ['-x'] }\n"
                )
            cfg = config_mod.Config(p)
            resolved = cfg.mcp_multiplex_servers_config.get("fetch") or {}
            self.assertEqual(resolved.get("command"), "/custom/fetch")


if __name__ == "__main__":
    unittest.main()
