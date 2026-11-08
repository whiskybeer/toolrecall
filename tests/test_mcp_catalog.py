"""Unit tests for the MCP gateway catalog (toolrecall.mcp_catalog).

The catalog is discovery data: names, descriptions, schemas, persisted so an
MCP `tools/list` never spawns a server. These tests pin the naming contract
(routing depends on it) and the never-raise behaviour on bad files.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest

import toolrecall.mcp_catalog as cat


class TestToolNaming(unittest.TestCase):
    def test_round_trip(self):
        name = cat.tool_name("hermes-docs", "search_docs")
        self.assertEqual(name, "mcp__hermes-docs__search_docs")
        self.assertEqual(cat.split_tool_name(name), ("hermes-docs", "search_docs"))

    def test_tool_name_may_contain_double_underscore(self):
        """Only the server name is reserved — tool names are split at the first __."""
        name = cat.tool_name("fetch", "fetch_url__batch")
        self.assertEqual(cat.split_tool_name(name), ("fetch", "fetch_url__batch"))

    def test_non_gateway_names_are_ignored(self):
        """Native tools must never be mistaken for gateway routes."""
        for name in ("read_file", "terminal", "mcp_call", "mcp_list_servers", ""):
            self.assertIsNone(cat.split_tool_name(name), name)

    def test_malformed_gateway_names(self):
        for name in ("mcp__", "mcp__server", "mcp__server__", "mcp____tool", "mcp__"):
            self.assertIsNone(cat.split_tool_name(name), name)


class TestCatalogIO(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.path = os.path.join(self.tmpdir, "mcp_tools_catalog.json")

    def test_missing_file_is_empty(self):
        self.assertEqual(cat.load(self.path), {})

    def test_corrupt_file_is_empty_not_fatal(self):
        """A torn/edited catalog must not break tools/list."""
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("{not json")
        self.assertEqual(cat.load(self.path), {})

    def test_wrong_shape_is_empty(self):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(["not", "a", "dict"], f)
        self.assertEqual(cat.load(self.path), {})

    def test_store_round_trip_and_merge(self):
        tools_a = [{"name": "search_docs", "description": "search"}]
        tools_b = [{"name": "list_tables"}]
        cat.store(self.path, "hermes-docs", tools_a)
        cat.store(self.path, "sqlite-browser", tools_b)
        data = cat.load(self.path)
        self.assertEqual(set(data), {"hermes-docs", "sqlite-browser"})
        self.assertEqual(data["hermes-docs"]["tools"], tools_a)
        self.assertIn("updated_at", data["hermes-docs"])

    def test_store_leaves_no_temp_file(self):
        cat.store(self.path, "time", [{"name": "get_time"}])
        self.assertFalse(os.path.exists(self.path + ".tmp"))

    def test_store_creates_parent_dir(self):
        nested = os.path.join(self.tmpdir, "sub", "dir", "catalog.json")
        cat.store(nested, "time", [{"name": "get_time"}])
        self.assertEqual(cat.load(nested)["time"]["tools"], [{"name": "get_time"}])


class TestCatalogDefs(unittest.TestCase):
    def test_defs_prefix_server_and_carry_schema(self):
        catalog = {
            "hermes-docs": {
                "tools": [
                    {
                        "name": "search_docs",
                        "description": "Full-text search",
                        "inputSchema": {
                            "type": "object",
                            "properties": {"query": {"type": "string"}},
                        },
                    }
                ]
            }
        }
        defs = cat.defs(catalog)
        self.assertEqual(len(defs), 1)
        self.assertEqual(defs[0]["name"], "mcp__hermes-docs__search_docs")
        self.assertTrue(defs[0]["description"].startswith("[hermes-docs]"))
        self.assertIn("query", defs[0]["inputSchema"]["properties"])

    def test_missing_schema_falls_back_to_permissive_object(self):
        defs = cat.defs({"time": {"tools": [{"name": "get_time"}]}})
        self.assertEqual(defs[0]["inputSchema"], {"type": "object", "properties": {}})
        # No description → the tool name is still meaningful to an agent.
        self.assertIn("get_time", defs[0]["description"])

    def test_nameless_and_broken_entries_are_skipped(self):
        catalog = {
            "a": {"tools": [{"description": "no name"}, {"name": "ok"}]},
            "b": {},  # server never listed successfully
            "c": {"tools": None},
        }
        defs = cat.defs(catalog)
        self.assertEqual([d["name"] for d in defs], ["mcp__a__ok"])

    def test_defs_are_stable_sorted(self):
        catalog = {
            "z": {"tools": [{"name": "z1"}]},
            "a": {"tools": [{"name": "a2"}, {"name": "a1"}]},
        }
        names = [d["name"] for d in cat.defs(catalog)]
        self.assertEqual(names, ["mcp__a__a2", "mcp__a__a1", "mcp__z__z1"])


class _FakeMultiplexCfg:
    """Just enough config surface for MCPMultiplexer.catalog()/warm_catalog()."""

    def __init__(
        self,
        db_path: str,
        servers_config: dict | None = None,
        allow: list | None = None,
        multiplex: bool = True,
        gateway: bool = False,
    ):
        self.cache_db = db_path
        self.mcp_multiplex_idle_minutes = 15
        self.mcp_multiplex_servers = ["hermes-docs"] if allow is None else allow
        self.mcp_multiplex_servers_config = (
            {
                "hermes-docs": {"command": "python3", "args": ["-m", "faketr"], "env": {}},
            }
            if servers_config is None
            else servers_config
        )
        self.mcp_multiplex_enabled = multiplex
        self.mcp_multiplex_gateway = gateway
        self.mcp_multiplex_gateway_exclude = []
        self.mcp_multiplex_gateway_exclude_tools = []


class TestMultiplexerCatalog(unittest.TestCase):
    """catalog() serves discovery data without ever starting a server.

    That is the whole point of persisting it: a cold `uv run` costs seconds and
    must not sit inside an MCP tools/list handshake.
    """

    def test_catalog_comes_from_disk_when_nothing_runs(self):
        from toolrecall.daemon import MCPMultiplexer

        tmpdir = tempfile.mkdtemp()
        cfg = _FakeMultiplexCfg(os.path.join(tmpdir, "cache.db"))
        cat.store(cat.catalog_path(cfg), "hermes-docs", [{"name": "search_docs"}])
        mux = MCPMultiplexer(cfg)
        self.assertEqual(mux.catalog()["hermes-docs"]["tools"], [{"name": "search_docs"}])
        self.assertEqual(mux._sessions, {}, "catalog() must not spawn a server")

    def test_catalog_is_empty_before_any_server_started(self):
        from toolrecall.daemon import MCPMultiplexer

        tmpdir = tempfile.mkdtemp()
        cfg = _FakeMultiplexCfg(os.path.join(tmpdir, "cache.db"))
        self.assertEqual(MCPMultiplexer(cfg).catalog(), {})


class TestGatewayWarmUp(unittest.TestCase):
    """A fresh install must not be stuck in a discovery deadlock.

    The gateway lists tools from a persisted catalog, and the catalog is only
    written when a server starts: nothing started -> nothing listed -> nobody
    calls -> never starts. warm_catalog() is the one best-effort pass that
    breaks it (run in a background thread by the daemon).
    """

    _THREE = {
        "hermes-docs": {"command": "python3", "args": ["-m", "a"], "env": {}},
        "fetch": {"command": "python3", "args": ["-m", "b"], "env": {}},
        "sqlite-browser": {"command": "python3", "args": ["-m", "c"], "env": {}},
    }

    def _mux(self, servers_config=None, allow=None, multiplex=True, gateway=True):
        from toolrecall.daemon import MCPMultiplexer

        tmpdir = tempfile.mkdtemp()
        cfg = _FakeMultiplexCfg(
            os.path.join(tmpdir, "cache.db"),
            servers_config=servers_config if servers_config is not None else self._THREE,
            allow=allow if allow is not None else [],
            multiplex=multiplex,
            gateway=gateway,
        )
        return MCPMultiplexer(cfg)

    def _stub(self, mux, fail=(), raise_for=()):
        calls = []

        def fake_ensure(name_lower):
            calls.append(name_lower)
            if name_lower in raise_for:
                raise RuntimeError("boom")
            if name_lower in fail:
                return f"{name_lower}: start failed — nope"
            return None

        mux._ensure_server = fake_ensure  # type: ignore[method-assign]
        return calls

    def test_gateway_enabled_requires_both_flags(self):
        self.assertTrue(self._mux(multiplex=True, gateway=True).gateway_enabled)
        self.assertFalse(self._mux(multiplex=True, gateway=False).gateway_enabled)
        self.assertFalse(self._mux(multiplex=False, gateway=True).gateway_enabled)

    def test_warmup_starts_every_configured_server(self):
        mux = self._mux()
        calls = self._stub(mux)
        outcome = mux.warm_catalog()
        self.assertEqual(sorted(calls), ["fetch", "hermes-docs", "sqlite-browser"])
        self.assertEqual(outcome["failed"], {})
        self.assertEqual(len(outcome["started"]), 3)

    def test_warmup_respects_the_server_allowlist(self):
        """Warming must not start servers the allowlist excludes."""
        mux = self._mux(allow=["fetch"])
        calls = self._stub(mux)
        outcome = mux.warm_catalog()
        self.assertEqual(calls, ["fetch"])
        self.assertEqual(outcome["started"], ["fetch"])

    def test_warmup_reports_a_failure_and_continues(self):
        mux = self._mux()
        self._stub(mux, fail={"fetch"})
        outcome = mux.warm_catalog()
        self.assertIn("fetch", outcome["failed"])
        self.assertEqual(sorted(outcome["started"]), ["hermes-docs", "sqlite-browser"])

    def test_warmup_survives_an_exception(self):
        """One broken server must not abort the pass or the thread."""
        mux = self._mux()
        self._stub(mux, raise_for={"sqlite-browser"})
        outcome = mux.warm_catalog()
        self.assertIn("RuntimeError: boom", outcome["failed"]["sqlite-browser"])
        self.assertEqual(sorted(outcome["started"]), ["fetch", "hermes-docs"])

    def test_wrapper_logs_and_never_raises(self):
        """The thread target swallows nothing silently: it logs."""
        from toolrecall.daemon import _warm_gateway_catalog

        class _Boom:
            def warm_catalog(self):
                raise RuntimeError("kaboom")

        logged: list[str] = []

        class _Log:
            def warning(self, msg, *args):
                logged.append(msg % args if args else msg)

            def info(self, *a, **k):
                pass

            def debug(self, *a, **k):
                pass

        _warm_gateway_catalog(_Boom(), _Log())
        self.assertTrue(logged)
        self.assertIn("kaboom", logged[0])


class TestGatewayExclusions(unittest.TestCase):
    """gateway_exclude / gateway_exclude_tools — surface control, not security.

    The gateway made write-capable tools first-class: `sqlite-browser` (pointed
    at the live cache DB) advertised write_query/create_table/append_insight to
    every session (2026-09-28). These tests pin the hiding behaviour; the docs
    and the function docstring state plainly that a hidden name is still
    callable, so nobody mistakes this for an enforcement boundary.
    """

    def _catalog(self) -> dict:
        return {
            "fetch": {"tools": [{"name": "fetch_url"}, {"name": "fetch_head"}]},
            "sqlite-browser": {
                "tools": [
                    {"name": "read_query"},
                    {"name": "write_query"},
                    {"name": "create_table"},
                ]
            },
        }

    def test_no_exclusions_returns_the_catalog_untouched(self):
        catalog = self._catalog()
        self.assertIs(cat.visible(catalog), catalog)

    def test_server_exclusion_drops_the_whole_server(self):
        out = cat.visible(self._catalog(), exclude_servers=["sqlite-browser"])
        self.assertNotIn("sqlite-browser", out)
        self.assertIn("fetch", out)

    def test_tool_exclusion_hides_that_name_everywhere(self):
        out = cat.visible(self._catalog(), exclude_tools=["write_query", "create_table"])
        names = [t["name"] for t in out["sqlite-browser"]["tools"]]
        self.assertEqual(names, ["read_query"])
        self.assertEqual(len(out["fetch"]["tools"]), 2)

    def test_exclusions_are_case_insensitive(self):
        out = cat.visible(
            self._catalog(),
            exclude_servers=["SQLite-Browser"],
            exclude_tools=["WRITE_QUERY", "Create_Table"],
        )
        self.assertNotIn("sqlite-browser", out)

    def test_unknown_names_are_a_noop(self):
        out = cat.visible(self._catalog(), exclude_servers=["nope"], exclude_tools=["nope"])
        self.assertEqual(sorted(out), ["fetch", "sqlite-browser"])
        self.assertEqual(len(out["sqlite-browser"]["tools"]), 3)

    def test_entry_metadata_survives_filtering(self):
        catalog = {"fetch": {"tools": [{"name": "fetch_url"}], "updated_at": 123.0}}
        out = cat.visible(catalog, exclude_tools=["nothing"])
        self.assertEqual(out["fetch"]["updated_at"], 123.0)

    def test_multiplexer_catalog_applies_configured_exclusions(self):
        """Integration: MCPMultiplexer.catalog() must honour the cfg lists.

        The server is explicitly configured here on purpose: an unconfigured
        server is dropped by the stale-entry filter, which would let this test
        pass for the wrong reason.
        """
        from toolrecall.daemon import MCPMultiplexer

        with tempfile.TemporaryDirectory() as td:
            cfg = _FakeMultiplexCfg(
                os.path.join(td, "cache.db"),
                servers_config={"sqlite-browser": {"command": "python3", "args": [], "env": {}}},
                allow=["sqlite-browser"],
            )
            cfg.mcp_multiplex_gateway_exclude_tools = ["write_query"]
            mux = MCPMultiplexer(cfg)
            cat.store(
                cat.catalog_path(cfg),
                "sqlite-browser",
                [{"name": "read_query"}, {"name": "write_query"}],
            )
            out = mux.catalog()
            names = [t["name"] for t in out["sqlite-browser"]["tools"]]
            self.assertEqual(names, ["read_query"])

    def test_multiplexer_catalog_honours_server_exclusion(self):
        from toolrecall.daemon import MCPMultiplexer

        with tempfile.TemporaryDirectory() as td:
            cfg = _FakeMultiplexCfg(
                os.path.join(td, "cache.db"),
                servers_config={"sqlite-browser": {"command": "python3", "args": [], "env": {}}},
                allow=["sqlite-browser"],
            )
            cfg.mcp_multiplex_gateway_exclude = ["sqlite-browser"]
            mux = MCPMultiplexer(cfg)
            cat.store(cat.catalog_path(cfg), "sqlite-browser", [{"name": "read_query"}])
            self.assertNotIn("sqlite-browser", mux.catalog())

    def test_only_servers_drops_unconfigured_entries(self):
        """A server removed from the config must stop being advertised.

        The catalog file is only rewritten when a server STARTS — so a stale
        entry (e.g. `hermes-docs` after switching to the in-repo `docs` server)
        would otherwise be advertised forever, with no route behind it.
        """
        out = cat.visible(self._catalog(), only_servers=["fetch"])
        self.assertEqual(sorted(out), ["fetch"])
        self.assertEqual(len(out["fetch"]["tools"]), 2)

    def test_only_servers_is_case_insensitive(self):
        out = cat.visible(self._catalog(), only_servers=["SQLITE-BROWSER"])
        self.assertEqual(sorted(out), ["sqlite-browser"])

    def test_multiplexer_catalog_drops_servers_no_longer_configured(self):
        from toolrecall.daemon import MCPMultiplexer

        with tempfile.TemporaryDirectory() as td:
            cfg = _FakeMultiplexCfg(os.path.join(td, "cache.db"))
            mux = MCPMultiplexer(cfg)
            path = cat.catalog_path(cfg)
            cat.store(path, "hermes-docs", [{"name": "search_docs"}])
            cat.store(path, "hermes-docs-stale", [{"name": "old_tool"}])
            out = mux.catalog()
            self.assertIn("hermes-docs", out)
            self.assertNotIn("hermes-docs-stale", out)


if __name__ == "__main__":
    unittest.main()
