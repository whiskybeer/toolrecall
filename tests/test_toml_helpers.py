"""tests/toml_helpers.py — the Windows-path TOML interpolation guard.

Background (a6 review, Windows/3.12 run): f-stringing a Windows path into
TOML — ``cache_db = "C:\\Users\\..."`` — makes ``\\U`` an invalid escape,
tomllib raises, ``Config._load`` warns and falls back to package defaults,
and every member silently shares one namespace. These tests pin the helper
contract: Windows-style paths round-trip through a TOML basic string, and
``write_toml_config`` fails loudly on a body that cannot parse.
"""

import os
import tempfile

import pytest

from tests.toml_helpers import toml_str, write_toml_config


class TestTomlStr:
    def test_windows_path_roundtrips(self):
        """The exact a6 failure input: drive letter + backslash components."""
        import tomllib

        p = r"C:\Users\robin\AppData\Local\Temp\pytest-of-x\test0"
        parsed = tomllib.loads(f"cache_db = {toml_str(p)}")
        assert parsed["cache_db"] == p

    def test_windows_path_with_backslash_u(self):
        r"""A literal ``\U`` sequence (user dir names like ``\Users``)."""
        import tomllib

        p = "C:\\Users\\dev\\x.db"
        parsed = tomllib.loads(f"cache_db = {toml_str(p)}")
        assert parsed["cache_db"] == p

    def test_posix_path_roundtrips(self):
        import tomllib

        parsed = tomllib.loads(f"cache_db = {toml_str('/tmp/x/c.db')}")
        assert parsed["cache_db"] == "/tmp/x/c.db"

    def test_posix_path_with_windows_inside(self):
        """POSIX temp dirs can contain Windows-shaped strings (wine paths)."""
        import tomllib

        wine = "/tmp/wine_c:\\Users"
        parsed = tomllib.loads(f"p = {toml_str(wine)}")
        assert parsed["p"] == wine


class TestWriteTomlConfig:
    def test_writes_and_parses(self, tmp_path):
        cfg = tmp_path / "c.toml"
        out = write_toml_config(cfg, '[paths]\ncache_db = "x.db"\n')
        assert os.path.realpath(out) == os.path.realpath(cfg)
        assert cfg.read_text(encoding="utf-8").startswith("[paths]")

    def test_invalid_toml_raises_at_setup(self, tmp_path):
        r"""The guard's whole point: invalid TOML never reaches assertions."""
        with pytest.raises(Exception):
            write_toml_config(tmp_path / "bad.toml", 'cache_db = "C:\\Users"')

    def test_windows_path_config_roundtrip(self, tmp_path):
        """End-to-end: a Windows-shaped config body parses where raw
        interpolation would have failed the whole file."""
        import tomllib

        win_tmp = r"C:\Users\robin\AppData\Local\Temp\pytest-of-x"
        body = (
            f"[paths]\ncache_db = {toml_str(win_tmp + '/c.db')}\n"
            f"[mcp]\nallowed_paths = [{toml_str(win_tmp)}]\n"
            f'[network.clients.alice]\ntoken = "t1"\n'
            f"allowed_paths = [{toml_str(win_tmp)}]\n"
        )
        cfg = write_toml_config(tmp_path / "win.toml", body)
        parsed = tomllib.load(open(cfg, "rb"))
        assert parsed["mcp"]["allowed_paths"] == [win_tmp]
        assert parsed["network"]["clients"]["alice"]["allowed_paths"] == [win_tmp]


def test_helper_is_importable_as_tests_package_member():
    """`from tests.toml_helpers import ...` — the same import path the
    a6/e2e fixtures use — resolves against the repo root on sys.path."""
    import tests.toml_helpers as h

    assert h.toml_str is toml_str
    assert tempfile.gettempdir()  # stdlib sanity, keeps flake8 import use
