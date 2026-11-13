"""One resolver for agent and product homes, shared by config and the shim.

The shim must resolve the same paths when config loading has *failed*, so the
cascades live in a stdlib-only module both can import. The agent cascade drifted
once as two copies (`Config.agent_home` and `shim._load_skip_prefixes`'s except
branch); the product data dir gets the same treatment for the same reason.
"""

import os
import unittest
from unittest import mock

from toolrecall import config as cfg
from toolrecall import home_dirs as hd
from toolrecall import shim as shim_mod

_ENV = ("AGENT_HOME", "TOOLRECALL_AGENT_HOME", "TOOLRECALL_DATA_DIR", "TOOLRECALL_CONFIG")


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
            self.assertEqual(hd.agent_home(), os.path.expanduser("~/agenthome"))

    def test_toolrecall_env_is_the_second_choice(self):
        with mock.patch.dict(os.environ, {"TOOLRECALL_AGENT_HOME": "/tmp/second"}):
            self.assertEqual(hd.agent_home(), "/tmp/second")

    def test_default_is_the_hermes_compat_path(self):
        self.assertEqual(hd.agent_home(), os.path.expanduser("~/.hermes"))

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

    def test_data_dir_and_the_shim_fallback_agree(self):
        for env, expected in (
            ({"TOOLRECALL_DATA_DIR": "/tmp/trdata"}, "/tmp/trdata"),
            ({}, os.path.expanduser("~/.toolrecall")),
        ):
            with mock.patch.dict(os.environ, env):
                self.assertEqual(cfg.Config().data_dir, expected, env)
                self.assertEqual(shim_mod._fallback_data_dir(), expected, env)


class DataDirTest(_EnvIsolated):
    """The product's own state dir: env > [paths].data_dir > ~/.toolrecall."""

    def test_default(self):
        self.assertEqual(hd.data_dir(), os.path.expanduser("~/.toolrecall"))

    def test_env_override(self):
        with mock.patch.dict(os.environ, {"TOOLRECALL_DATA_DIR": "~/tr-state"}):
            self.assertEqual(hd.data_dir(), os.path.expanduser("~/tr-state"))

    def test_paths_setting_is_honoured(self):
        cfg_obj = cfg.Config()
        cfg_obj._data.setdefault("paths", {})["data_dir"] = "/var/lib/toolrecall"
        self.assertEqual(cfg_obj.data_dir, "/var/lib/toolrecall")

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
