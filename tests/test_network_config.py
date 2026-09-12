"""Tests for [network] config schema (A6 Task 2.1).

Covers: safe defaults (off, loopback), env-var parity, token gating,
TLS config, and the A3 known-sections warning for network keys.
"""

import pytest

from toolrecall.config import Config


@pytest.fixture(autouse=True)
def _no_network_env(monkeypatch):
    """Isolate tests from ambient TOOLRECALL_NETWORK_* environment."""
    for var in (
        "TOOLRECALL_NETWORK_ENABLED",
        "TOOLRECALL_NETWORK_BIND",
        "TOOLRECALL_NETWORK_PORT",
        "TOOLRECALL_NETWORK_TOKEN",
        "TOOLRECALL_NETWORK_TLS",
        "TOOLRECALL_NETWORK_TLS_CERT",
        "TOOLRECALL_NETWORK_TLS_KEY",
    ):
        monkeypatch.delenv(var, raising=False)


def _write_config(tmp_path, body: str) -> Config:
    cfg_file = tmp_path / "toolrecall.toml"
    cfg_file.write_text(body)
    return Config(str(cfg_file))


class TestNetworkDefaults:
    def test_disabled_by_default(self):
        cfg = Config.__new__(Config)  # skip file loading
        cfg._data = {}
        cfg._expand_paths()
        assert cfg.network_enabled is False

    def test_bind_loopback_by_default(self):
        cfg = Config.__new__(Config)
        cfg._data = {}
        cfg._expand_paths()
        assert cfg.network_bind == "127.0.0.1"

    def test_default_port_8570(self):
        cfg = Config.__new__(Config)
        cfg._data = {}
        cfg._expand_paths()
        assert cfg.network_port == 8570

    def test_token_empty_by_default(self):
        cfg = Config.__new__(Config)
        cfg._data = {}
        cfg._expand_paths()
        assert cfg.network_token == ""

    def test_tls_off_by_default(self):
        cfg = Config.__new__(Config)
        cfg._data = {}
        cfg._expand_paths()
        assert cfg.network_tls is False
        assert cfg.network_tls_cert is None
        assert cfg.network_tls_key is None


class TestNetworkConfigFile:
    def test_enabled_via_toml(self, tmp_path):
        cfg = _write_config(tmp_path, "[network]\nenabled = true\nport = 9000\n")
        assert cfg.network_enabled is True
        assert cfg.network_port == 9000

    def test_tls_section(self, tmp_path):
        cfg = _write_config(
            tmp_path,
            '[network]\ntls = true\ntls_cert = "/tmp/c.pem"\ntls_key = "/tmp/k.pem"\n',
        )
        assert cfg.network_tls is True
        assert cfg.network_tls_cert == "/tmp/c.pem"
        assert cfg.network_tls_key == "/tmp/k.pem"

    def test_invalid_port_falls_back_to_default(self, tmp_path):
        cfg = _write_config(tmp_path, '[network]\nport = "not-a-number"\n')
        assert cfg.network_port == 8570


class TestNetworkEnvParity:
    def test_env_enables(self, monkeypatch):
        monkeypatch.setenv("TOOLRECALL_NETWORK_ENABLED", "true")
        cfg = Config.__new__(Config)
        cfg._data = {}
        cfg._expand_paths()
        from toolrecall.config import _apply_env_overrides

        cfg._data = _apply_env_overrides(cfg._data)
        assert cfg.network_enabled is True

    def test_env_token(self, monkeypatch):
        monkeypatch.setenv("TOOLRECALL_NETWORK_TOKEN", "  s3cret  ")
        cfg = Config.__new__(Config)
        cfg._data = {}
        cfg._expand_paths()
        from toolrecall.config import _apply_env_overrides

        cfg._data = _apply_env_overrides(cfg._data)
        assert cfg.network_token == "s3cret"

    def test_env_bool_false(self, monkeypatch):
        monkeypatch.setenv("TOOLRECALL_NETWORK_ENABLED", "false")
        cfg = Config.__new__(Config)
        cfg._data = {}
        cfg._expand_paths()
        from toolrecall.config import _apply_env_overrides

        cfg._data = _apply_env_overrides(cfg._data)
        assert cfg.network_enabled is False


class TestNetworkClients:
    def test_default_deny_when_no_clients(self):
        cfg = Config.__new__(Config)
        cfg._data = {}
        cfg._expand_paths()
        assert cfg.network_clients == {}

    def test_clients_table_parsed(self, tmp_path):
        cfg = _write_config(
            tmp_path,
            '[network.clients."ci-runner"]\n'
            'allowed_paths = ["~/projects"]\n'
            "allow_terminal = false\n",
        )
        assert cfg.network_clients == {
            "ci-runner": {"allowed_paths": ["~/projects"], "allow_terminal": False}
        }


class TestKnownSectionsWarning:
    def test_network_section_not_flagged_as_unknown(self, tmp_path):
        """A3 gate: a valid [network] config must not trigger the typo warning."""
        import warnings

        cfg_file = tmp_path / "toolrecall.toml"
        cfg_file.write_text("[network]\nenabled = true\n")
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            Config(str(cfg_file))
        unknown = [x for x in w if "unknown config section" in str(x.message)]
        assert not unknown, f"unexpected unknown-section warnings: {unknown}"
