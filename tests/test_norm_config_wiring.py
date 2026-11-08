"""B1 dead-code-audit fix: [norm] config wiring into the normalizer.

[norm].sort_lists / [norm].strip_strings were plumbed through ENV_MAP, the
known-keys schema and the norm_* Config properties, but NO consumer ever read
the properties: normalize_tool_args() always ran on the hard-coded module
defaults (both True), so a user's env/TOML override changed nothing — cache
keys came out identical with and without the config.

Pins:
  * a TOML config with sort_lists=false flips the normalizer module default;
  * the TOOLRECALL_NORM_SORT_LISTS env override flips it;
  * per-call overrides still win over configured module defaults;
  * a programmatic configure() after config load still wins;
  * the MCP cache key (cached_mcp_check) reflects the config end-to-end —
    proof the wiring reaches a real consumer, not just module state.

Standalone-runnable; restores module state and env after every test.
"""

import os
import shutil
import tempfile
import unittest


class TestNormConfigWiring(unittest.TestCase):
    """Module-default wiring: TOML + env -> normalizer globals."""

    ENV_KEYS = ("TOOLRECALL_NORM_SORT_LISTS", "TOOLRECALL_NORM_STRIP_STRINGS")

    def setUp(self):
        import toolrecall.normalizer as norm

        self._norm = norm
        self._saved = (norm._SORT_LISTS, norm._STRIP_STRINGS)
        self._saved_env = {k: os.environ.get(k) for k in self.ENV_KEYS}

    def tearDown(self):
        self._norm.configure(sort_lists=self._saved[0], strip_strings=self._saved[1])
        for k, v in self._saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _config(self, toml_body: str):
        from toolrecall.config import Config

        tmp = tempfile.mkdtemp(prefix="tr_norm_")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        path = os.path.join(tmp, "toolrecall.toml")
        with open(path, "w") as f:
            f.write(toml_body)
        return Config(path)

    def test_toml_sort_lists_false_flips_module_default(self):
        cfg = self._config("[norm]\nsort_lists = false\n")
        self.assertFalse(cfg.norm_sort_lists)
        self.assertFalse(self._norm._SORT_LISTS)
        self.assertTrue(self._norm._STRIP_STRINGS)  # sibling key untouched
        # Module default actually honored: list order preserved now.
        self.assertEqual(self._norm.normalize_json({"k": ["b", "a"]}), '{"k":["b","a"]}')

    def test_env_override_flips_module_default(self):
        os.environ["TOOLRECALL_NORM_SORT_LISTS"] = "false"
        self._config("")  # empty user file; env override must reach the module
        self.assertFalse(self._norm._SORT_LISTS)

    def test_percall_overrides_still_win_over_config(self):
        self._config("[norm]\nsort_lists = false\nstrip_strings = false\n")
        self.assertFalse(self._norm._SORT_LISTS)
        # Configured default: strip OFF.
        self.assertEqual(self._norm.normalize_json({"k": "  x  "}), '{"k":"  x  "}')
        # Per-call override wins over the configured default.
        self.assertEqual(
            self._norm.normalize_json({"k": "  x  "}, strip_strings=True),
            '{"k":"x"}',
        )
        self.assertEqual(
            self._norm.normalize_json({"k": ["b", "a"]}, sort_lists=True),
            '{"k":["a","b"]}',
        )

    def test_configure_after_load_still_wins(self):
        """Precedence order: config < configure() < per-call args."""
        self._config("[norm]\nsort_lists = false\n")
        self._norm.configure(sort_lists=True)
        self.assertTrue(self._norm._SORT_LISTS)


class TestNormWiringReachesCacheKey(unittest.TestCase):
    """End-to-end: cached_mcp_check's request key changes with [norm] config.

    DB-isolated (TOOLRECALL_CACHE_DB + singleton reset) so the key lookups
    never touch the production cache.db.
    """

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.mkdtemp(prefix="tr_normkey_")
        os.environ["TOOLRECALL_CACHE_DB"] = os.path.join(cls._tmp, "normkey.db")
        import toolrecall._db as _db

        with _db._db_lock:
            if _db._db_real is not None:
                _db._db_real.close()
            _db._db_real = None
            _db._db_path_cached = None
            _db._cached_config = None
        from toolrecall.cache import _init

        _init()

    @classmethod
    def tearDownClass(cls):
        import toolrecall._db as _db

        with _db._db_lock:
            if _db._db_real is not None:
                _db._db_real.close()
            _db._db_real = None
            _db._db_path_cached = None
            _db._cached_config = None
        os.environ.pop("TOOLRECALL_CACHE_DB", None)
        shutil.rmtree(cls._tmp, ignore_errors=True)

    ARGS = {"flags": ["-l", "-a"]}

    def _config(self, toml_body: str):
        from tests.toml_helpers import toml_str, write_toml_config

        tmp = tempfile.mkdtemp(prefix="tr_normkey_cfg_")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        toml = (
            f"[paths]\ncache_db = {toml_str(os.path.join(self._tmp, 'normkey.db'))}\n" + toml_body
        )
        cfg_file = os.path.join(tmp, "toolrecall.toml")
        write_toml_config(cfg_file, toml)
        from toolrecall.config import Config

        return Config(cfg_file)

    def _key(self, cfg):
        from toolrecall import cache as cache_mod
        from toolrecall.cache import cached_mcp_check

        # cached_mcp_check reads cache.py's module-global config (loaded at
        # import). Point it at this test's config — exactly what happens in
        # a real process where the user config carries [norm].
        old = cache_mod.config
        cache_mod.config = cfg
        self.addCleanup(setattr, cache_mod, "config", old)
        return cached_mcp_check("srv", "tool", dict(self.ARGS))["key"]

    def test_norm_enabled_sort_lists_false_changes_mcp_cache_key(self):
        from toolrecall.normalizer import configure

        try:
            key_default = self._key(self._config(""))

            cfg_sorted = self._config("[norm]\nenabled = true\n")
            configure(sort_lists=True, strip_strings=True)  # deterministic defaults
            key_sorted = self._key(cfg_sorted)

            cfg_unsorted = self._config("[norm]\nenabled = true\nsort_lists = false\n")
            # Config construction just wired sort_lists=false into the module;
            # prove the wiring (not a leftover) drives the key.
            import toolrecall.normalizer as norm

            self.assertFalse(norm._SORT_LISTS)
            key_unsorted = self._key(cfg_unsorted)
            # Determinism: same config -> same key (recompute while the
            # wiring is still in effect; _key restores state via addCleanup).
            key_repeat = self._key(cfg_unsorted)
        finally:
            configure(sort_lists=True, strip_strings=True)  # restore for siblings

        self.assertNotEqual(key_default, key_sorted)
        self.assertNotEqual(key_default, key_unsorted)
        self.assertNotEqual(key_sorted, key_unsorted)
        self.assertEqual(key_unsorted, key_repeat)

    def test_norm_disabled_uses_plain_json_key(self):
        """norm.enabled=false -> plain json.dumps(sort_keys) key, as before."""
        from toolrecall import cache
        from toolrecall.normalizer import configure

        try:
            configure(sort_lists=True, strip_strings=True)
            cfg = self._config("[norm]\nenabled = false\n")
            key = self._key(cfg)
        finally:
            configure(sort_lists=True, strip_strings=True)
        # Plain path: json.dumps keeps ': ' separators (spaces), unlike the
        # compact normalizer output.
        expected = cache._hash('srv://tool?{"flags": ["-l", "-a"]}')
        self.assertEqual(key, expected)


if __name__ == "__main__":
    unittest.main()
