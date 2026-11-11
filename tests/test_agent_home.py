"""One agent-home resolver, shared by config and the shim's failure path.

The shim must resolve the same home when config loading has *failed*, so the
cascade lives in a stdlib-only module both can import. It drifted once as two
copies (`Config.agent_home` and `shim._load_skip_prefixes`'s except branch).
"""

import os
import unittest
from unittest import mock

from toolrecall import agent_home as ah
from toolrecall import config as cfg
from toolrecall import shim as shim_mod

_ENV = ("AGENT_HOME", "TOOLRECALL_AGENT_HOME", "TOOLRECALL_CONFIG")


class _EnvIsolated(unittest.TestCase):
    def setUp(self):
        saved = {k: os.environ.pop(k, None) for k in _ENV}

        def _restore():
            for k in _ENV:
                os.environ.pop(k, None)
            for k, v in saved.items():
                if v is not None:
                    os.environ[k] = v

        self.addCleanup(_restore)


class ResolverTest(_EnvIsolated):
    def test_env_var_wins_and_is_expanded(self):
        with mock.patch.dict(os.environ, {"AGENT_HOME": "~/agenthome"}):
            self.assertEqual(ah.resolve(), os.path.expanduser("~/agenthome"))

    def test_toolrecall_env_is_the_second_choice(self):
        with mock.patch.dict(os.environ, {"TOOLRECALL_AGENT_HOME": "/tmp/second"}):
            self.assertEqual(ah.resolve(), "/tmp/second")

    def test_default_is_the_hermes_compat_path(self):
        self.assertEqual(ah.resolve(), os.path.expanduser("~/.hermes"))

    def test_config_and_the_shim_fallback_agree(self):
        """Both sides of the contract: config load OK, and config load broken."""
        for env, expected in (
            ({"AGENT_HOME": "/tmp/agent-x"}, "/tmp/agent-x"),
            ({"TOOLRECALL_AGENT_HOME": "/tmp/agent-y"}, "/tmp/agent-y"),
            ({}, os.path.expanduser("~/.hermes")),
        ):
            with mock.patch.dict(os.environ, env):
                self.assertEqual(cfg.Config().agent_home, expected, env)
                self.assertEqual(shim_mod._fallback_agent_home(), expected, env)

    def test_paths_setting_loses_to_env_and_wins_over_default(self):
        tmp_cfg = os.path.join(os.path.dirname(__file__), "_no_such_config.toml")
        self.assertFalse(os.path.exists(tmp_cfg))
        with mock.patch.dict(os.environ, {"TOOLRECALL_CONFIG": tmp_cfg}):
            cfg_obj = cfg.Config()
            # No file → default; then a [paths] value via the in-memory map.
            cfg_obj._data.setdefault("paths", {})["agent_home"] = "/tmp/from-config"
            self.assertEqual(cfg_obj.agent_home, "/tmp/from-config")
            with mock.patch.dict(os.environ, {"AGENT_HOME": "/tmp/env-wins"}):
                self.assertEqual(cfg_obj.agent_home, "/tmp/env-wins")


if __name__ == "__main__":
    unittest.main()
