# Copyright (c) 2026 Robin Schultka
# SPDX-License-Identifier: MIT
# Source: https://github.com/whiskybeer/toolrecall
"""MCP cache correctness: per-request TTL and side-effecting tools.

Two live-class bugs, fixed 2026-09-28:

  A — a per-request TTL was ignored. The bridge sends ``ttl=0`` for
  ``bypass_cache``; the daemon read only the server config, so a caller
  explicitly asking for a fresh read was served the cached body and had no
  way to tell (no error, no flag) — a silent stale delivery.

  B — side-effecting tools were cached. No read/write classification existed,
  so a repeated ``create_issue`` inside the TTL returned the stored reply and
  *never reached the server*. That is a correctness bug, not a missed saving:
  the caller believes the action happened.

The fix classifies a tool by the server's own ``readOnlyHint`` annotation when
it has one, else by read/write verbs in the name; a non-cacheable tool is never
served from cache, never stored, and its call invalidates that server's cached
reads (the write may have changed what those rows describe).
"""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

from toolrecall import cache as cache_mod
from toolrecall import daemon as daemon_mod


class TestToolClassification(unittest.TestCase):
    """cache.mcp_tool_is_cacheable — annotations win, else name verbs."""

    def test_write_verbs_are_not_cacheable(self) -> None:
        for tool in ("create_issue", "delete_file", "update_pr", "push", "run_query_plan"):
            assert cache_mod.mcp_tool_is_cacheable(tool) is False, tool

    def test_reading_verbs_are_cacheable(self) -> None:
        for tool in ("list_issues", "get_issue", "search_docs", "read_query", "fetch_url"):
            assert cache_mod.mcp_tool_is_cacheable(tool) is True, tool

    def test_leading_read_verb_beats_a_write_noun(self) -> None:
        # "get_commit" / "list_labels" contain write-looking nouns but only read.
        assert cache_mod.mcp_tool_is_cacheable("get_commit") is True
        assert cache_mod.mcp_tool_is_cacheable("list_labels") is True

    def test_camel_and_dotted_names_split(self) -> None:
        assert cache_mod.mcp_tool_is_cacheable("createIssue") is False
        assert cache_mod.mcp_tool_is_cacheable("issues.create") is False
        assert cache_mod.mcp_tool_is_cacheable("issues.list") is True

    def test_readonly_hint_decides_both_ways(self) -> None:
        # An opaque name the heuristic would cache, but the server says it writes.
        assert cache_mod.mcp_tool_is_cacheable("mutate", {"readOnlyHint": False}) is False
        # A write-named tool the server declares read-only (e.g. a dry run).
        assert cache_mod.mcp_tool_is_cacheable("delete_preview", {"readOnlyHint": True}) is True

    def test_destructive_hint_blocks_caching(self) -> None:
        assert cache_mod.mcp_tool_is_cacheable("widget", {"destructiveHint": True}) is False

    def test_opaque_name_without_annotations_stays_cacheable(self) -> None:
        """The pre-existing behaviour for read-only servers with odd names
        (e.g. docs_search) is preserved — the docstring records the residual
        risk, and the daemon's invalidate-on-write self-heals it."""
        assert cache_mod.mcp_tool_is_cacheable("docs_search") is True
        assert cache_mod.mcp_tool_is_cacheable("ping") is True


class _FakeMux:
    """Multiplexer stand-in exposing only what _handle_mcp_call touches."""

    def __init__(self, annotations: dict | None = None, default_ttl: int = 60) -> None:
        self.cfg = mock.Mock()
        self.cfg.mcp_multiplex_transparent_cache = True
        self.cfg.mcp_multiplex_default_ttl = default_ttl
        self.cfg.mcp_multiplex_servers_config = {}
        self.cfg.mcp_multiplex_fetch_revalidate = False
        self._annotations = annotations or {}
        self.calls: list[tuple] = []

    def tool_annotations(self, server: str, tool: str):
        return self._annotations.get(tool.lower())

    def call(self, server: str, tool: str, arguments: dict):
        self.calls.append((server, tool, arguments))
        return {"ok": True, "server": server, "tool": tool}


def _daemon(tmp_dir: str, mux: _FakeMux) -> daemon_mod.DaemonServer:
    d = daemon_mod.DaemonServer(socket_path=os.path.join(tmp_dir, "t.sock"))
    d._server = None
    d.multiplexer = mux
    # The security gate is exercised by its own tests; here it only has to let
    # the call through (a bare Mock returns truthy Mocks, which read as
    # "blocked").
    d.security = mock.Mock()
    for gate in (
        "check_mcp_server",
        "check_mcp_tool_access",
        "cognitive_scan_arguments",
        "check_ast_injection",
    ):
        getattr(d.security, gate).return_value = None
    return d


class TestPerRequestTTL(unittest.TestCase):
    """Bug A: a per-request ttl must win over the server config."""

    def test_bypass_ttl_zero_reaches_the_check(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            d = _daemon(td, _FakeMux())
            seen: dict = {}

            def _check(server, tool, arguments, ttl=None):
                seen["ttl"] = ttl
                return {"cached": False, "key": "k"}

            with (
                mock.patch.object(daemon_mod, "_cache_mcp_check", _check),
                mock.patch.object(daemon_mod, "_cache_mcp_store"),
            ):
                d._handle_mcp_call(
                    {
                        "cmd": "mcp_call",
                        "server": "fetch",
                        "tool": "fetch_url",
                        "arguments": {"url": "u"},
                        "ttl": 0,
                    }
                )
            assert seen["ttl"] == 0, f"bypass ttl was not honoured: {seen['ttl']!r}"

    def test_server_ttl_used_when_request_has_none(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            mux = _FakeMux()
            mux.cfg.mcp_multiplex_servers_config = {"fetch": {"ttl": 300}}
            d = _daemon(td, mux)
            seen: dict = {}

            def _check(server, tool, arguments, ttl=None):
                seen["ttl"] = ttl
                return {"cached": False, "key": "k"}

            with (
                mock.patch.object(daemon_mod, "_cache_mcp_check", _check),
                mock.patch.object(daemon_mod, "_cache_mcp_store"),
            ):
                d._handle_mcp_call(
                    {"cmd": "mcp_call", "server": "fetch", "tool": "fetch_url", "arguments": {}}
                )
            assert seen["ttl"] == 300, f"server ttl lost: {seen['ttl']!r}"

    def test_garbage_ttl_falls_back_to_server_config(self) -> None:
        """A non-numeric or bool ttl must not crash or zero the policy."""
        with tempfile.TemporaryDirectory() as td:
            d = _daemon(td, _FakeMux())
            seen: dict = {}

            def _check(server, tool, arguments, ttl=None):
                seen["ttl"] = ttl
                return {"cached": False, "key": "k"}

            with (
                mock.patch.object(daemon_mod, "_cache_mcp_check", _check),
                mock.patch.object(daemon_mod, "_cache_mcp_store"),
            ):
                for bad in ("0", True, None, []):
                    d._handle_mcp_call(
                        {
                            "cmd": "mcp_call",
                            "server": "fetch",
                            "tool": "fetch_url",
                            "arguments": {},
                            "ttl": bad,
                        }
                    )
            assert seen["ttl"] == 60, f"garbage ttl leaked through: {seen['ttl']!r}"


class TestWriteToolsAreNeverCached(unittest.TestCase):
    """Bug B: a side-effecting tool is never served from nor written to cache."""

    def test_write_tool_skips_the_cache_check(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            d = _daemon(td, _FakeMux())
            check = mock.Mock(return_value={"cached": True, "data": "STALE", "key": "k"})
            with (
                mock.patch.object(daemon_mod, "_cache_mcp_check", check),
                mock.patch.object(daemon_mod, "_cache_mcp_store") as store,
                mock.patch.object(daemon_mod, "_cache_invalidate_mcp_server") as inv,
            ):
                out = d._handle_mcp_call(
                    {"cmd": "mcp_call", "server": "github", "tool": "create_issue", "args": {}}
                )
            check.assert_not_called()
            store.assert_not_called()
            assert out.get("cached") is False
            # The live call must actually have happened.
            assert d.multiplexer.calls and d.multiplexer.calls[0][1] == "create_issue"
            # And the server's cached reads are dropped.
            inv.assert_called_once_with("github")

    def test_read_tool_is_still_cached(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            d = _daemon(td, _FakeMux())
            with (
                mock.patch.object(
                    daemon_mod, "_cache_mcp_check", mock.Mock(return_value={"key": "k"})
                ),
                mock.patch.object(daemon_mod, "_cache_mcp_store") as store,
                mock.patch.object(daemon_mod, "_cache_invalidate_mcp_server") as inv,
            ):
                d._handle_mcp_call(
                    {"cmd": "mcp_call", "server": "github", "tool": "list_issues", "args": {}}
                )
            store.assert_called_once()
            inv.assert_not_called()

    def test_annotation_turns_a_read_named_tool_into_a_write(self) -> None:
        """A cold server is classified by name; the annotation seen after the
        call must still block the STORE (else the first call pins a write)."""
        with tempfile.TemporaryDirectory() as td:
            mux = _FakeMux(annotations={"frobnicate": {"readOnlyHint": False}})
            d = _daemon(td, mux)
            # Pre-check happens with annotations already present, so the check
            # is skipped entirely; either way the reply must not be stored.
            with (
                mock.patch.object(daemon_mod, "_cache_mcp_check") as check,
                mock.patch.object(daemon_mod, "_cache_mcp_store") as store,
                mock.patch.object(daemon_mod, "_cache_invalidate_mcp_server") as inv,
            ):
                d._handle_mcp_call(
                    {"cmd": "mcp_call", "server": "srv", "tool": "frobnicate", "args": {}}
                )
            check.assert_not_called()
            store.assert_not_called()
            inv.assert_called_once_with("srv")

    def test_readonly_annotation_permits_caching_a_write_named_tool(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            mux = _FakeMux(annotations={"delete_preview": {"readOnlyHint": True}})
            d = _daemon(td, mux)
            with (
                mock.patch.object(
                    daemon_mod, "_cache_mcp_check", mock.Mock(return_value={"key": "k"})
                ),
                mock.patch.object(daemon_mod, "_cache_mcp_store") as store,
                mock.patch.object(daemon_mod, "_cache_invalidate_mcp_server") as inv,
            ):
                d._handle_mcp_call(
                    {"cmd": "mcp_call", "server": "srv", "tool": "delete_preview", "args": {}}
                )
            store.assert_called_once()
            inv.assert_not_called()

    def test_transparent_cache_off_means_no_invalidation(self) -> None:
        """With the transparent cache disabled there is no cache to protect —
        and no invalidation (which would otherwise write to the DB per call)."""
        with tempfile.TemporaryDirectory() as td:
            mux = _FakeMux()
            mux.cfg.mcp_multiplex_transparent_cache = False
            d = _daemon(td, mux)
            with (
                mock.patch.object(daemon_mod, "_cache_mcp_store") as store,
                mock.patch.object(daemon_mod, "_cache_invalidate_mcp_server") as inv,
            ):
                d._handle_mcp_call(
                    {"cmd": "mcp_call", "server": "srv", "tool": "create_issue", "args": {}}
                )
            store.assert_not_called()
            inv.assert_not_called()

    def test_failed_write_still_invalidates(self) -> None:
        """Deliberate: an errored write may have applied partially, and assumed
        no-op is the unsafe direction — a refetch is cheap, a stale read is not."""
        with tempfile.TemporaryDirectory() as td:
            mux = _FakeMux()
            mux.call = lambda *a: {"error": "boom"}  # type: ignore[method-assign]
            d = _daemon(td, mux)
            with (
                mock.patch.object(daemon_mod, "_cache_mcp_store") as store,
                mock.patch.object(daemon_mod, "_cache_invalidate_mcp_server") as inv,
            ):
                d._handle_mcp_call(
                    {"cmd": "mcp_call", "server": "srv", "tool": "create_issue", "args": {}}
                )
            store.assert_not_called()
            inv.assert_called_once_with("srv")


class TestAnnotationsCapture(unittest.TestCase):
    """The multiplexer records tools/list annotations once a server starts."""

    def test_tool_annotations_returns_none_for_unknown_server(self) -> None:
        from toolrecall.daemon import MCPMultiplexer

        mux = MCPMultiplexer.__new__(MCPMultiplexer)
        mux._annotations = {}
        mux._lock = __import__("threading").Lock()
        assert mux.tool_annotations("nope", "anything") is None

    def test_tool_annotations_is_case_insensitive(self) -> None:
        from toolrecall.daemon import MCPMultiplexer

        mux = MCPMultiplexer.__new__(MCPMultiplexer)
        mux._annotations = {"github": {"list_issues": {"readOnlyHint": True}}}
        mux._lock = __import__("threading").Lock()
        assert mux.tool_annotations("GitHub", "LIST_ISSUES") == {"readOnlyHint": True}
        assert mux.tool_annotations("github", "unknown_tool") is None


if __name__ == "__main__":
    unittest.main()
