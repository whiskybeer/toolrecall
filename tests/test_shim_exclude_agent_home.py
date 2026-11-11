"""RED-first tests for the default shim exclude list (P3).

Reads under the agent home (``config.yaml``, ``cron/jobs.json``, plugin
manifests) are already tagged ``source="shim"`` by ``_LIBRARY_DIR_MARKERS``, so
they never count toward ``tokens_saved_to_llm`` — but the shim still intercepted
them and wrote an ``access_log`` row: 25,952 of 26,076 rows on 2026-11-09
(99.5 %), driven by cron workers reloading their own control plane.

The default must therefore cover the agent home, resolved through the existing
``Config.agent_home`` cascade (AGENT_HOME → TOOLRECALL_AGENT_HOME →
[paths].agent_home → ~/.hermes), so nothing is hardcoded here.
"""

import os
import tempfile
import unittest
from unittest import mock

from toolrecall import config as config_mod
from toolrecall import shim as shim_mod

_ENV_KEYS = (
    "AGENT_HOME",
    "TOOLRECALL_AGENT_HOME",
    "TOOLRECALL_SHIM_EXCLUDE_PREFIXES",
    "TOOLRECALL_CONFIG",
)


def _write_toml(text: str) -> str:
    fd, path = tempfile.mkstemp(prefix="tr_p3_", suffix=".toml")
    with os.fdopen(fd, "w") as f:
        f.write(text)
    return path


class _Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="tr_p3_")
        self.addCleanup(self._tmp.cleanup)
        self.home = os.path.join(self._tmp.name, "agenthome")
        os.makedirs(self.home)
        # Isolate from the machine's own env (this box has an explicit exclude
        # list in ~/.config, which would mask the default under test).
        saved = {k: os.environ.pop(k, None) for k in _ENV_KEYS}

        def _restore_env():
            for k in _ENV_KEYS:
                os.environ.pop(k, None)
            for k, v in saved.items():
                if v is not None:
                    os.environ[k] = v

        self.addCleanup(_restore_env)
        self.addCleanup(setattr, shim_mod, "_SKIP_PREFIXES", None)
        shim_mod._SKIP_PREFIXES = None

    def _cfg(self, toml: str = "[shim]\n"):
        path = _write_toml(toml)
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        return config_mod.Config(path)


class DefaultPrefixesTest(_Base):
    def test_default_includes_the_agent_home(self):
        with mock.patch.dict(os.environ, {"AGENT_HOME": self.home}):
            prefixes = self._cfg().shim_exclude_prefixes
        self.assertEqual(prefixes, ["/proc/", "/sys/", "/dev/", self.home + "/"])

    def test_toolrecall_agent_home_is_honored(self):
        with mock.patch.dict(os.environ, {"TOOLRECALL_AGENT_HOME": self.home}):
            prefixes = self._cfg().shim_exclude_prefixes
        self.assertIn(self.home + "/", prefixes)

    def test_paths_agent_home_setting_is_honored(self):
        prefixes = self._cfg(f'[paths]\nagent_home = "{self.home}"\n').shim_exclude_prefixes
        self.assertIn(self.home + "/", prefixes)

    def test_a_root_agent_home_is_never_added(self):
        # "/" would skip every path and disable the shim entirely.
        with mock.patch.dict(os.environ, {"AGENT_HOME": "/"}):
            prefixes = self._cfg().shim_exclude_prefixes
        self.assertEqual(prefixes, ["/proc/", "/sys/", "/dev/"])

    def test_explicit_list_replaces_the_default(self):
        cfg = self._cfg('[shim]\nexclude_prefixes = ["/tmp/only-this/"]\n')
        self.assertEqual(cfg.shim_exclude_prefixes, ["/tmp/only-this/"])

    def test_explicit_empty_list_still_bypasses_nothing(self):
        cfg = self._cfg("[shim]\nexclude_prefixes = []\n")
        self.assertEqual(cfg.shim_exclude_prefixes, [])


class ShouldSkipTest(_Base):
    """The default must actually keep the agent home out of the shim."""

    def _load(self):
        with mock.patch.dict(os.environ, {"AGENT_HOME": self.home}):
            with mock.patch.object(config_mod, "load_config", lambda *a, **k: self._cfg()):
                shim_mod._SKIP_PREFIXES = None
                shim_mod._load_skip_prefixes()

    def test_control_plane_reads_bypass_the_shim(self):
        self._load()
        self.assertTrue(shim_mod._should_skip(os.path.join(self.home, "config.yaml")))
        self.assertTrue(shim_mod._should_skip(os.path.join(self.home, "cron", "jobs.json")))

    def test_workspace_reads_still_go_through_the_shim(self):
        self._load()
        self.assertFalse(shim_mod._should_skip(os.path.join(self._tmp.name, "proj", "main.py")))

    def test_opt_out_restores_full_interception(self):
        with mock.patch.object(
            config_mod, "load_config", lambda *a, **k: self._cfg("[shim]\nexclude_prefixes = []\n")
        ):
            shim_mod._SKIP_PREFIXES = None
            shim_mod._load_skip_prefixes()
        self.assertFalse(shim_mod._should_skip(os.path.join(self.home, "config.yaml")))


if __name__ == "__main__":
    unittest.main()
