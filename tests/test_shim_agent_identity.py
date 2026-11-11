"""Agent identity is a CONTRACT, not a vendor list.

The shim's context-tier tagging and the Lever-1 stub only fire in processes it
considers agent sessions. Before this split the only signal was a hardcoded
prefix list in ``shim._AGENT_ENV_MARKERS``: harnesses missing from it silently
got no stubs and no CONTEXT-tier accounting, and adding a harness meant editing
core (design principle: Agnostic). The supported path is now
``TOOLRECALL_AGENT_ID`` / ``TOOLRECALL_AGENT_SESSION``; the vendor list stays as
a zero-config fallback.
"""

import os

import pytest

import toolrecall.shim as shim

_VENDOR_PREFIXES = (
    "TOOLRECALL_AGENT",
    "HERMES_",
    "CLAUDECODE",
    "CLAUDE_CODE_ENTRYPOINT",
    "CURSOR_TRACE_ID",
    "AIDER_",
    "CODEX_",
    "OPENCODE_",
)


def _clean_env(monkeypatch, **env: str) -> None:
    """Remove agent-ish markers, set exactly the ones a test wants, reset cache."""
    for key in list(os.environ):
        if key.startswith(_VENDOR_PREFIXES):
            monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(shim, "_agent_process_cache", None, raising=False)


def test_contract_arms_without_any_vendor_marker(monkeypatch):
    _clean_env(monkeypatch, TOOLRECALL_AGENT_ID="my-own-loop")
    assert shim._agent_identity() == "my-own-loop"
    assert shim._env_has_agent_marker() is True
    assert shim._process_is_agent() is True


def test_agent_session_flag_is_equivalent_opt_in(monkeypatch):
    _clean_env(monkeypatch, TOOLRECALL_AGENT_SESSION="1")
    assert shim._agent_identity() == "1"
    assert shim._env_has_agent_marker() is True


def test_vendor_marker_still_works_as_zero_config_fallback(monkeypatch):
    _clean_env(monkeypatch, HERMES_SESSION_ID="abc123")
    assert shim._agent_identity() is None
    assert shim._env_has_agent_marker() is True


@pytest.mark.parametrize("value", ["0", "false", "no", "off", "  "])
def test_falsy_contract_values_do_not_arm(monkeypatch, value):
    _clean_env(monkeypatch, TOOLRECALL_AGENT_ID=value)
    assert shim._agent_identity() is None
    assert shim._env_has_agent_marker() is False


def test_no_contract_and_no_marker_is_not_an_agent(monkeypatch):
    _clean_env(monkeypatch)
    assert shim._agent_identity() is None
    assert shim._env_has_agent_marker() is False
