"""Tests for shim source tagging (CONTEXT tier): shim reads as agent_tool.

The shim serves an agent's file reads; when the content enters LLM
context those hits must count toward tokens_not_sent_to_llm. Tagging is
a conservative AND-gate (agent process + non-library path + text suffix
+ size cap), fail-closed to "shim".

Covers:
  - _tag_source_for() gate: agent env, library paths, binary suffixes,
    size cap, mode overrides (always/never), invalid mode fallback
  - _shim_open() routing: hit in agent process → agent_tool reach the
    client layer (mocked), miss → fall-through, exactly-once stats
  - config plumbing: [shim].tag_source registered, env override
"""

import builtins
import os
import sys
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from toolrecall import shim as shim_mod
from toolrecall.shim import _tag_source_for


def _agent_env(monkeypatch_like):
    """Set one known agent marker; return the setter for teardown lists."""
    monkeypatch_like.setenv("HERMES_SESSION_ID", "test-session")


class TestTagSourceGate(unittest.TestCase):
    def setUp(self):
        import tempfile

        self._tmpdir = tempfile.mkdtemp()
        # Cache resets — every test resolves env/mode fresh.
        self._saved = (shim_mod._agent_process_cache, shim_mod._tag_source_mode_cache)
        shim_mod._agent_process_cache = None
        shim_mod._tag_source_mode_cache = None
        # Detach from the outer agent's env (Hermes, Claude Code, Codex, …):
        # assertions must hold for the SYNTHETIC environment, not the real one.
        self._saved_environ = os.environ.copy()
        for key in [k for k in os.environ if k.startswith(shim_mod._AGENT_ENV_MARKERS)]:
            del os.environ[key]
        # The harness-neutral activation contract is an agent signal too
        # (2026-11-11): assertions must not depend on whether the developer's
        # own shell exports it.
        for key in ("TOOLRECALL_AGENT_ID", "TOOLRECALL_AGENT_SESSION"):
            os.environ.pop(key, None)
        os.environ.pop("TOOLRECALL_SHIM_TAG_SOURCE", None)

    def tearDown(self):
        shim_mod._agent_process_cache, shim_mod._tag_source_mode_cache = self._saved
        os.environ.clear()
        os.environ.update(self._saved_environ)

    # ── agent-process detection ──

    def test_agent_env_marker_tags_agent_tool(self):
        os.environ["HERMES_SESSION_ID"] = "s"
        self.assertEqual(_tag_source_for("/home/u/project/main.py"), "agent_tool")

    def test_claude_code_marker_tags_agent_tool(self):
        os.environ["CLAUDECODE"] = "1"
        self.assertEqual(_tag_source_for("/home/u/project/main.py"), "agent_tool")

    def test_non_agent_process_stays_shim(self):
        # No agent markers at all → shim (fail closed).
        self.assertEqual(_tag_source_for("/home/u/project/main.py"), "shim")

    # ── library-path gate ──

    def test_site_packages_stays_shim(self):
        os.environ["HERMES_SESSION_ID"] = "s"
        self.assertEqual(
            _tag_source_for("/home/u/.venv/lib/python3.11/site-packages/requests/api.py"),
            "shim",
        )

    def test_node_modules_stays_shim(self):
        os.environ["HERMES_SESSION_ID"] = "s"
        self.assertEqual(_tag_source_for("/home/u/proj/node_modules/left-pad/index.js"), "shim")

    def test_agent_home_config_stays_shim(self):
        os.environ["HERMES_SESSION_ID"] = "s"
        self.assertEqual(_tag_source_for("/home/u/.hermes/webui/sessions/abc.json"), "shim")

    # ── binary-suffix gate ──

    def test_binary_suffix_stays_shim(self):
        os.environ["HERMES_SESSION_ID"] = "s"
        self.assertEqual(_tag_source_for("/home/u/proj/logo.png"), "shim")
        self.assertEqual(_tag_source_for("/home/u/proj/data.parquet"), "shim")

    # ── size cap ──

    def test_oversized_file_stays_shim(self):
        os.environ["HERMES_SESSION_ID"] = "s"
        big = os.path.join(self._tmpdir, "big.py")
        with open(big, "wb") as f:
            f.write(b"x = 1\n" * (300 * 1024))
        self.assertEqual(_tag_source_for(big), "shim")

    def test_small_file_within_cap(self):
        os.environ["HERMES_SESSION_ID"] = "s"
        small = os.path.join(self._tmpdir, "small.py")
        with open(small, "w") as f:
            f.write("x = 1\n")
        self.assertEqual(_tag_source_for(small), "agent_tool")

    # ── mode overrides ──

    def test_mode_always_forces_agent_tool(self):
        os.environ["TOOLRECALL_SHIM_TAG_SOURCE"] = "always"
        # No agent env, library path — still forced.
        self.assertEqual(_tag_source_for("/home/u/.venv/lib/x/y.py"), "agent_tool")

    def test_mode_never_forces_shim(self):
        os.environ["HERMES_SESSION_ID"] = "s"
        os.environ["TOOLRECALL_SHIM_TAG_SOURCE"] = "never"
        self.assertEqual(_tag_source_for("/home/u/proj/main.py"), "shim")

    def test_invalid_mode_falls_back_to_auto(self):
        os.environ["HERMES_SESSION_ID"] = "s"
        os.environ["TOOLRECALL_SHIM_TAG_SOURCE"] = "bogus"
        self.assertEqual(_tag_source_for("/home/u/proj/main.py"), "agent_tool")

    # ── caches ──

    def test_agent_process_cache_prevents_repeated_env_scan(self):
        os.environ["HERMES_SESSION_ID"] = "s"
        self.assertTrue(shim_mod._process_is_agent())
        # Remove marker; cached result must hold (per-process semantics).
        del os.environ["HERMES_SESSION_ID"]
        self.assertTrue(shim_mod._process_is_agent())

    def test_tag_mode_cached(self):
        os.environ["TOOLRECALL_SHIM_TAG_SOURCE"] = "never"
        self.assertEqual(shim_mod._tag_source_mode(), "never")
        os.environ["TOOLRECALL_SHIM_TAG_SOURCE"] = "always"
        self.assertEqual(shim_mod._tag_source_mode(), "never")  # cached


class TestShimOpenRouting(unittest.TestCase):
    """_shim_open passes the gated source through to the read function."""

    def setUp(self):
        self._tmpdir = os.path.realpath(os.path.join(__import__("tempfile").mkdtemp(), ""))
        self._saved = (
            shim_mod._agent_process_cache,
            shim_mod._tag_source_mode_cache,
            shim_mod._TR,
            shim_mod._original_open,
            shim_mod._SKIP_PREFIXES,
        )
        shim_mod._agent_process_cache = None
        shim_mod._tag_source_mode_cache = None
        shim_mod._TR = None
        shim_mod._SKIP_PREFIXES = []
        self._saved_environ = os.environ.copy()
        for key in [k for k in os.environ if k.startswith("HERMES_")]:
            del os.environ[key]

    def tearDown(self):
        (
            shim_mod._agent_process_cache,
            shim_mod._tag_source_mode_cache,
            shim_mod._TR,
            shim_mod._original_open,
            shim_mod._SKIP_PREFIXES,
        ) = self._saved
        os.environ.clear()
        os.environ.update(self._saved_environ)
        shim_mod._thread_local.active = False
        import shutil

        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_hit_uses_gated_source(self):
        os.environ["HERMES_SESSION_ID"] = "s"
        seen = {}

        def read(path, source="", **kwargs):
            seen["source"] = source
            return {"cached": True, "content": "cached content"}

        shim_mod._TR = {"read": read}
        result = shim_mod._shim_open(os.path.join(self._tmpdir, "main.py"), "r")
        self.assertEqual(result.read(), "cached content")
        result.close()
        self.assertEqual(seen["source"], "agent_tool")

    def test_hit_library_path_uses_shim_source(self):
        os.environ["HERMES_SESSION_ID"] = "s"
        seen = {}

        def read(path, source="", **kwargs):
            seen["source"] = source
            return {"cached": True, "content": "lib"}

        shim_mod._TR = {"read": read}
        lib = os.path.join(self._tmpdir, "site-packages", "x.py")
        os.makedirs(os.path.dirname(lib), exist_ok=True)
        result = shim_mod._shim_open(lib, "r")
        result.close()
        self.assertEqual(seen["source"], "shim")

    def test_miss_source_is_still_gated(self):
        # Miss: source tagging applies to the read that POPULATES the cache.
        os.environ["HERMES_SESSION_ID"] = "s"
        seen = {}

        def read(path, source="", **kwargs):
            seen["source"] = source
            return {"cached": False, "content": ""}

        shim_mod._TR = {"read": read}
        real_file = os.path.join(self._tmpdir, "main.py")
        with open(real_file, "w") as f:
            f.write("real")
        shim_mod._original_open = MagicMock(
            return_value=builtins.open(real_file)  # noqa: SIM115 — test handle
        )
        result = shim_mod._shim_open(real_file, "r")
        result.close()
        self.assertEqual(seen["source"], "agent_tool")
        shim_mod._original_open.assert_called_once()

    def test_exactly_one_read_call_per_open(self):
        # No double-call regression: the gate must run pre-call.
        os.environ["HERMES_SESSION_ID"] = "s"
        calls = []

        def read(path, source="", **kwargs):
            calls.append(source)
            return {"cached": True, "content": "content"}

        shim_mod._TR = {"read": read}
        result = shim_mod._shim_open(os.path.join(self._tmpdir, "main.py"), "r")
        result.close()
        self.assertEqual(len(calls), 1)


class TestConfigPlumbing(unittest.TestCase):
    def test_tag_source_registered_in_known_keys(self):
        from toolrecall.config import Config

        self.assertIn("tag_source", Config._KNOWN["shim"])

    def test_env_map_has_shim_tag_source(self):
        from toolrecall.config import ENV_MAP

        self.assertEqual(ENV_MAP.get("TOOLRECALL_SHIM_TAG_SOURCE"), ("shim", "tag_source"))


if __name__ == "__main__":
    unittest.main()
