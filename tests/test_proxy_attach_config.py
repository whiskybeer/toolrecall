"""Regression tests for `toolrecall proxy attach` config line-editing.

Covers the bug where a config whose LLM section carried no `base_url:` line
made attach a silent no-op ("no llm.base_url found ... skipping") instead of
inserting the line — so `toolrecall stop` / a partial revert left the agent
permanently unproxied while attach reported a failure the operator had to
notice by hand.

Live-instance protection: every test pins TOOLRECALL_PROXY_TARGET at a
tmp_path config and sets HOME to tmp_path, so the real ~/.hermes/config.yaml
is unreachable even if a candidate lookup goes wrong. `_proxy_alive()` is
stubbed, so results never depend on the live daemon on :8569.
"""

import os

import pytest

from toolrecall import cli

PROXY_BASE = "http://127.0.0.1:8569/v1"

MODEL_WITHOUT_BASE_URL = """\
model:
  provider: openrouter
  default: some-model
  context_length: 1000000
agent:
  name: test
"""


@pytest.fixture(autouse=True)
def _isolated_agent_config(tmp_path, monkeypatch):
    """Keep every test off the live config, the live proxy and AGENT_HOME."""
    monkeypatch.setenv("TOOLRECALL_FORWARD_PORT", "8569")
    monkeypatch.delenv("AGENT_HOME", raising=False)
    monkeypatch.delenv("TOOLRECALL_AGENT_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(cli, "_proxy_alive", lambda timeout=2.0: True)
    return tmp_path


def _write(tmp_path, text):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(text)
    return cfg


def _base_url_lines(path):
    return [ln for ln in path.read_text().splitlines() if ln.strip().startswith("base_url:")]


def test_inserts_base_url_when_section_has_none(tmp_path, monkeypatch):
    cfg = _write(tmp_path, MODEL_WITHOUT_BASE_URL)
    monkeypatch.setenv("TOOLRECALL_PROXY_TARGET", str(cfg))

    ok, msg = cli._attach_hermes_to_proxy()

    assert ok is True, msg
    assert "inserted" in msg
    text = cfg.read_text()
    assert "  base_url: " + PROXY_BASE in text  # child of model:, not top-level
    assert text.count("base_url:") == 1
    # the keys that were already there survive untouched
    assert "  provider: openrouter" in text
    assert "agent:\n  name: test" in text


def test_insert_uses_sibling_indent(tmp_path, monkeypatch):
    cfg = _write(tmp_path, "model:\n    provider: openrouter\n    default: m\n")
    monkeypatch.setenv("TOOLRECALL_PROXY_TARGET", str(cfg))

    ok, _ = cli._attach_hermes_to_proxy()

    assert ok is True
    assert "    base_url: " + PROXY_BASE in cfg.read_text()


def test_insert_into_section_with_no_children(tmp_path, monkeypatch):
    cfg = _write(tmp_path, "model:\nagent:\n  name: test\n")
    monkeypatch.setenv("TOOLRECALL_PROXY_TARGET", str(cfg))

    ok, _ = cli._attach_hermes_to_proxy()

    assert ok is True
    assert (
        cfg.read_text()
        == "model:\n  base_url: "
        + PROXY_BASE
        + "  # toolrecall proxy (detach: toolrecall proxy detach)\nagent:\n  name: test\n"
    )


def test_updates_existing_base_url_and_records_previous(tmp_path, monkeypatch):
    cfg = _write(
        tmp_path, "model:\n  base_url: https://api.openrouter.ai/api/v1\n  provider: openrouter\n"
    )
    monkeypatch.setenv("TOOLRECALL_PROXY_TARGET", str(cfg))

    ok, msg = cli._attach_hermes_to_proxy()

    assert ok is True, msg
    assert "updated" in msg
    lines = _base_url_lines(cfg)
    assert len(lines) == 1
    assert lines[0].startswith("  base_url: " + PROXY_BASE)
    assert "was https://api.openrouter.ai/api/v1" in lines[0]


def test_insert_writes_backup_of_original(tmp_path, monkeypatch):
    cfg = _write(tmp_path, MODEL_WITHOUT_BASE_URL)
    monkeypatch.setenv("TOOLRECALL_PROXY_TARGET", str(cfg))

    ok, _ = cli._attach_hermes_to_proxy()

    assert ok is True
    backup = str(cfg) + ".toolrecall-bak"
    assert os.path.exists(backup)
    assert open(backup).read() == MODEL_WITHOUT_BASE_URL


def test_refuses_without_llm_section(tmp_path, monkeypatch):
    original = "agent:\n  name: test\n"
    cfg = _write(tmp_path, original)
    monkeypatch.setenv("TOOLRECALL_PROXY_TARGET", str(cfg))

    ok, msg = cli._attach_hermes_to_proxy()

    assert ok is False
    assert "no model:/llm: section" in msg
    assert cfg.read_text() == original
    assert not os.path.exists(str(cfg) + ".toolrecall-bak")


def test_refuses_when_proxy_is_down(tmp_path, monkeypatch):
    cfg = _write(tmp_path, MODEL_WITHOUT_BASE_URL)
    monkeypatch.setenv("TOOLRECALL_PROXY_TARGET", str(cfg))
    monkeypatch.setattr(cli, "_proxy_alive", lambda timeout=2.0: False)

    ok, msg = cli._attach_hermes_to_proxy()

    assert ok is False
    assert "not reachable" in msg
    assert cfg.read_text() == MODEL_WITHOUT_BASE_URL  # untouched
    assert not os.path.exists(str(cfg) + ".toolrecall-bak")


def test_already_proxied_is_a_noop(tmp_path, monkeypatch):
    original = (
        "model:\n  base_url: " + PROXY_BASE + "  # toolrecall proxy\n  provider: openrouter\n"
    )
    cfg = _write(tmp_path, original)
    monkeypatch.setenv("TOOLRECALL_PROXY_TARGET", str(cfg))

    ok, msg = cli._attach_hermes_to_proxy()

    assert ok is True
    assert "already" in msg
    assert cfg.read_text() == original
    assert not os.path.exists(str(cfg) + ".toolrecall-bak")


def test_no_config_found(tmp_path, monkeypatch):
    monkeypatch.setenv("TOOLRECALL_PROXY_TARGET", str(tmp_path / "absent.yaml"))

    ok, msg = cli._attach_hermes_to_proxy()

    assert ok is False
    assert "no agent config found" in msg


def test_detach_restores_an_inserted_base_url(tmp_path, monkeypatch):
    cfg = _write(tmp_path, MODEL_WITHOUT_BASE_URL)
    monkeypatch.setenv("TOOLRECALL_PROXY_TARGET", str(cfg))

    ok, _ = cli._attach_hermes_to_proxy()
    assert ok is True
    rc = cli._detach_hermes_from_proxy()

    assert rc == 0
    assert cfg.read_text() == MODEL_WITHOUT_BASE_URL
    assert not os.path.exists(str(cfg) + ".toolrecall-bak")
