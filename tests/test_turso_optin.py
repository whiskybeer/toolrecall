"""Tests for Turso sync opt-in gating and turso_cli helpers.

These run WITHOUT libsql-experimental and WITHOUT network — they cover
the security-relevant plumbing: sync is off by default, config flips
work, .env upserts don't accumulate tokens, and secret files get 0600.
"""

import os
import re
import stat
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from toolrecall.turso_cli import _upsert_env_lines, _write_private, _set_sync_enabled
from toolrecall import _db


class TestSyncOptInGating(unittest.TestCase):
    """db_sync() must be a no-op unless explicitly enabled."""

    def setUp(self):
        self._orig = dict(os.environ)
        _db._cached_config = None

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._orig)
        _db._cached_config = None

    def test_sync_disabled_by_default_on_sqlite(self):
        os.environ["TOOLRECALL_STORAGE_BACKEND"] = "sqlite"
        _db._cached_config = None
        self.assertFalse(_db.db_sync())

    def test_sync_requires_explicit_enable_even_with_credentials(self):
        # backend=sqlite so we never touch libsql — the gating checks
        # run in order and backend is checked first; here we verify the
        # config properties directly for the libsql case.
        os.environ["TOOLRECALL_SYNC_URL"] = "libsql://x.turso.io"
        os.environ["TOOLRECALL_SYNC_TOKEN"] = "tok"
        _db._cached_config = None
        from toolrecall.config import load_config
        cfg = load_config()
        # url+token set, but master switch is off by default:
        self.assertFalse(cfg.libsql_sync_enabled)

    def test_sync_enabled_env_coercion(self):
        from toolrecall.config import load_config
        for raw, expected in [("true", True), ("1", True), ("yes", True),
                              ("false", False), ("0", False), ("", False)]:
            os.environ["TOOLRECALL_SYNC_ENABLED"] = raw
            cfg = load_config()
            self.assertEqual(cfg.libsql_sync_enabled, expected, f"raw={raw!r}")
        os.environ.pop("TOOLRECALL_SYNC_ENABLED")

    def test_turso_api_base_customizable(self):
        from toolrecall.config import load_config
        self.assertEqual(load_config().turso_api_base, "https://api.turso.tech")
        os.environ["TOOLRECALL_TURSO_API_BASE"] = "https://turso.internal.example/"
        cfg = load_config()
        self.assertEqual(cfg.turso_api_base, "https://turso.internal.example")


class TestEnvUpsert(unittest.TestCase):
    """Re-running init must not accumulate stale tokens in .env."""

    def test_upsert_replaces_existing_keys(self):
        existing = "# comment\nTOOLRECALL_SYNC_TOKEN=old-revoked-token\nOTHER=keep\n"
        out = _upsert_env_lines(existing, {"TOOLRECALL_SYNC_TOKEN": "new-token"})
        self.assertIn("TOOLRECALL_SYNC_TOKEN=new-token", out)
        self.assertNotIn("old-revoked-token", out)
        self.assertIn("OTHER=keep", out)
        self.assertIn("# comment", out)

    def test_upsert_appends_missing_keys(self):
        out = _upsert_env_lines("A=1\n", {"TOOLRECALL_SYNC_URL": "libsql://x"})
        self.assertIn("A=1", out)
        self.assertIn("TOOLRECALL_SYNC_URL=libsql://x", out)

    def test_upsert_idempotent(self):
        once = _upsert_env_lines("", {"K": "v"})
        twice = _upsert_env_lines(once, {"K": "v"})
        self.assertEqual(once, twice)
        self.assertEqual(twice.count("K=v"), 1)


class TestPrivateFileWrite(unittest.TestCase):
    def test_write_private_sets_0600(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "secret.toml")
            _write_private(p, "sync_token = \"s\"\n")
            mode = stat.S_IMODE(os.stat(p).st_mode)
            self.assertEqual(mode, 0o600)
            with open(p) as f:
                self.assertIn("sync_token", f.read())

    def test_write_private_tightens_existing_file(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "loose.env")
            with open(p, "w") as f:
                f.write("X=1\n")
            os.chmod(p, 0o644)
            _write_private(p, "X=2\n")
            self.assertEqual(stat.S_IMODE(os.stat(p).st_mode), 0o600)


class TestSetSyncEnabled(unittest.TestCase):
    def _run_with_config(self, initial: str, value: bool) -> str:
        with tempfile.TemporaryDirectory() as d:
            cfg = os.path.join(d, "toolrecall.toml")
            with open(cfg, "w") as f:
                f.write(initial)
            with mock.patch("os.path.expanduser", return_value=cfg):
                _set_sync_enabled(value)
            with open(cfg) as f:
                return f.read()

    def test_flip_existing_flag(self):
        out = self._run_with_config("[storage]\nsync_enabled = false\n", True)
        self.assertRegex(out, r"(?m)^sync_enabled = true")
        out = self._run_with_config("[storage]\nsync_enabled = true  # note\n", False)
        self.assertRegex(out, r"(?m)^sync_enabled = false")
        self.assertIn("# note", out)  # trailing comment preserved

    def test_insert_flag_when_missing(self):
        out = self._run_with_config("[storage]\nbackend = \"libsql\"\n", True)
        self.assertRegex(out, r"(?m)^sync_enabled = true")
        self.assertIn('backend = "libsql"', out)


class TestBlocklistCoversOwnSecrets(unittest.TestCase):
    """ToolRecall must refuse to cache-read its own token/DB files."""

    def test_config_dir_blocked(self):
        self.assertTrue(_db._is_sensitive_path("~/.config/toolrecall/toolrecall.toml"))
        self.assertTrue(_db._is_sensitive_path("~/.config/toolrecall/.env"))

    def test_cache_dbs_blocked(self):
        self.assertTrue(_db._is_sensitive_path("~/.toolrecall/cache.db"))
        self.assertTrue(_db._is_sensitive_path("~/.toolrecall/cache-libsql.db"))
        self.assertTrue(_db._is_sensitive_path("~/.toolrecall/cache-libsql.db-wal"))

    def test_normal_files_not_blocked(self):
        self.assertFalse(_db._is_sensitive_path("/tmp/project/main.py"))
        self.assertFalse(_db._is_sensitive_path("~/.toolrecall/README.md"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
