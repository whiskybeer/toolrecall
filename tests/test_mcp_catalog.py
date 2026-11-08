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
    """Just enough config surface for MCPMultiplexer.catalog()."""

    def __init__(self, db_path: str):
        self.cache_db = db_path
        self.mcp_multiplex_idle_minutes = 15
        self.mcp_multiplex_servers = ["hermes-docs"]
        self.mcp_multiplex_servers_config = {
            "hermes-docs": {"command": "python3", "args": ["-m", "faketr"], "env": {}}
        }


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


if __name__ == "__main__":
    unittest.main()
