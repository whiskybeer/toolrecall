"""Tests for the Hermes `toolrecall-light` co-install detector in healthcheck."""

from __future__ import annotations

import os
from typing import cast

from toolrecall import healthcheck


def test_no_plugin_reported_for_clean_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    assert healthcheck.detect_light_plugin() == {
        "installed": False,
        "enabled": False,
        "path": "",
    }


def test_installed_but_not_enabled(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "plugins" / "toolrecall-light").mkdir(parents=True)
    (tmp_path / "config.yaml").write_text("plugins:\n  enabled: []\n", encoding="utf-8")
    result = healthcheck.detect_light_plugin()
    assert result["installed"] is True
    assert result["enabled"] is False
    assert result["path"].endswith("plugins/toolrecall-light")


def test_enabled_via_plugins_list(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(
        "plugins:\n  enabled:\n    - other-plugin\n    - toolrecall-light\n", encoding="utf-8"
    )
    result = healthcheck.detect_light_plugin()
    assert result["enabled"] is True
    assert result["installed"] is True


def test_explicit_home_argument_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", os.path.join(str(tmp_path), "ignored"))
    home = tmp_path / "explicit"
    home.mkdir()
    (home / "config.yaml").write_text(
        "plugins:\n  enabled:\n    - toolrecall-light\n", encoding="utf-8"
    )
    assert healthcheck.detect_light_plugin(str(home))["enabled"] is True


def test_similarly_named_plugins_do_not_match(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(
        "plugins:\n  enabled:\n    - toolrecall-light-fork\n", encoding="utf-8"
    )
    assert healthcheck.detect_light_plugin()["enabled"] is False


def test_missing_config_is_not_an_error(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "nope"))
    assert healthcheck.detect_light_plugin()["installed"] is False


def _healthy_info(**overrides):
    info = {
        "procs": 1,
        "pid_file": "123",
        "pid_alive": True,
        "lock_files": 0,
        "socket_path": "/tmp/x.sock",
        "socket": True,
        "proxy": True,
        "proxy_port": 8569,
        "shim_state": healthcheck.SHIM_ACTIVE,
        "shim_reason": "",
        "shim_probe_s": None,
        "shim_venvs": [],
        "shim_bare": [],
        "shim_bare_required": [],
        "hit_rate": 0.5,
        "log_file": "",
        "log_errors_recent": 0,
        "log_warnings_recent": 0,
        "hermes_plugin": {"installed": False, "enabled": False, "path": ""},
    }
    info.update(overrides)
    return info


def test_warning_names_the_plugin_and_the_switch_off_command():
    info = _healthy_info(hermes_plugin={"installed": True, "enabled": True, "path": "/x"})
    notes = healthcheck.warnings(cast(healthcheck.HealthInfo, info))
    joined = " ".join(notes)
    assert "toolrecall-light" in joined
    assert "hermes plugins disable toolrecall-light" in joined


def test_no_extra_warning_when_plugin_absent():
    notes = healthcheck.warnings(cast(healthcheck.HealthInfo, _healthy_info()))
    assert not any("toolrecall-light" in note for note in notes)


def test_gather_includes_the_key(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    info = healthcheck.gather()
    assert "hermes_plugin" in info
    assert info["hermes_plugin"]["enabled"] is False
