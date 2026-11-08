"""allow_symlink_escapes default = false (review 2026-09).

The F1 Layer-B lexical rescue must be OPT-IN on a stock install: the
attack against it (writer inside an allowed dir publishing a symlink,
junction reparse on Windows) is easiest on Windows, so the safe default
is strict realpath-only containment. These tests pin the default across
every fallback surface so a future refactor can't silently flip it back.
"""

import os
import sys
import tempfile
import unittest

_test_db_dir = tempfile.mkdtemp()
os.environ["TOOLRECALL_CACHE_DB"] = os.path.join(_test_db_dir, "test_symlink_default.db")

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from toolrecall.config import Config
from toolrecall.daemon import SecurityGate
from toolrecall.path_utils import check_path_allowed


def _cfg(toml_text: str) -> Config:
    d = tempfile.mkdtemp()
    p = os.path.join(d, "toolrecall.toml")
    with open(p, "w") as f:
        f.write(toml_text)
    return Config(p)


class TestSymlinkDefaultOff(unittest.TestCase):
    def test_config_property_default_false(self):
        cfg = _cfg("")  # no [security] section at all
        self.assertIs(cfg.mcp_allow_symlink_escapes, False)

    def test_config_env_false_wins(self):
        os.environ["TOOLRECALL_ALLOW_SYMLINK_ESCAPES"] = "0"
        try:
            cfg = _cfg("[security]\nallow_symlink_escapes = true\n")
            self.assertIs(cfg.mcp_allow_symlink_escapes, False)
        finally:
            del os.environ["TOOLRECALL_ALLOW_SYMLINK_ESCAPES"]

    def test_config_env_true_opts_in(self):
        os.environ["TOOLRECALL_ALLOW_SYMLINK_ESCAPES"] = "1"
        try:
            cfg = _cfg("")
            self.assertIs(cfg.mcp_allow_symlink_escapes, True)
        finally:
            del os.environ["TOOLRECALL_ALLOW_SYMLINK_ESCAPES"]

    def test_package_template_ships_false(self):
        import tomllib

        tmpl = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "toolrecall",
            "config.toml",
        )
        with open(tmpl, "rb") as f:
            data = tomllib.load(f)
        self.assertIs(data["security"]["allow_symlink_escapes"], False)

    def test_default_config_object_matches_property(self):
        """Config defaults and toml_serializer defaults must agree."""
        from toolrecall.toml_serializer import DEFAULT_CONFIG

        self.assertIs(DEFAULT_CONFIG["security"]["allow_symlink_escapes"], False)

    def test_daemon_gate_strict_on_stock_config(self):
        """A daemon built from a default config must run Layer A only."""
        tmp = tempfile.mkdtemp()
        real = os.path.join(tmp, "real")
        os.makedirs(real)
        allowed = os.path.join(tmp, "allowed")
        os.makedirs(allowed)
        link = os.path.join(allowed, "escape")
        os.symlink(real, link)

        cfg = _cfg(f'[mcp]\nallowed_paths = ["{allowed}"]\n')
        gate = SecurityGate(cfg)
        self.assertIs(gate.allow_symlink_escapes, False)
        # Layer A still grants realpath-contained reads (file physically
        # INSIDE the allowed dir — no symlink involved):
        inside = os.path.join(allowed, "plain.txt")
        with open(inside, "w") as f:
            f.write("x")
        self.assertIsNone(gate.check_read_path(inside))
        # Layer B rescue is OFF by default: lexical-inside, target-outside denied.
        err = gate.check_read_path(os.path.join(link, "f.txt"))
        self.assertIsNotNone(err, "stock install must NOT grant the Layer-B rescue")
        # Opting in restores it:
        cfg2 = _cfg(
            f'[mcp]\nallowed_paths = ["{allowed}"]\n[security]\nallow_symlink_escapes = true\n'
        )
        self.assertIsNone(SecurityGate(cfg2).check_read_path(os.path.join(link, "f.txt")))


class TestPathUtilsSignature(unittest.TestCase):
    def test_helper_default_false(self):
        import inspect

        sig = inspect.signature(check_path_allowed)
        self.assertIs(sig.parameters["allow_symlink_escapes"].default, False)

    def test_escape_denied_by_default_escape_allowed_optin(self):
        tmp = tempfile.mkdtemp()
        real = os.path.join(tmp, "real")
        os.makedirs(real)
        allowed = os.path.join(tmp, "allowed")
        os.makedirs(allowed)
        link = os.path.join(allowed, "escape")
        os.symlink(real, link)
        target = os.path.join(link, "secret.txt")

        self.assertFalse(check_path_allowed(target, [allowed]))
        self.assertTrue(check_path_allowed(target, [allowed], True))


if __name__ == "__main__":
    unittest.main()
