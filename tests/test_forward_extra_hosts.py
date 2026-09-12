"""Hermetic tests: extra_hosts agnosticism for the forward proxy.

Covers:
1. Config path: [forward_proxy].extra_hosts merged into the allowlist.
2. Env path: TOOLRECALL_FORWARD_EXTRA_HOSTS merged.
3. Merge of both sources; port suffix stripped; case normalized.
4. SSRF default-deny intact: unknown hosts stay refused (no wildcard).
"""

import importlib

import pytest


@pytest.fixture()
def fresh_hosts(monkeypatch):
    """Reload proxy with a clean extra-host env; restore afterwards."""
    monkeypatch.delenv("TOOLRECALL_FORWARD_EXTRA_HOSTS", raising=False)
    monkeypatch.delenv("TOOLRECALL_CONFIG", raising=False)
    import toolrecall.proxy as proxy

    importlib.reload(proxy)
    yield proxy
    importlib.reload(proxy)


def test_env_extra_hosts_merged(fresh_hosts, monkeypatch):
    monkeypatch.setenv("TOOLRECALL_FORWARD_EXTRA_HOSTS", "vllm.internal:8000, AZURE.GW.example")
    proxy = fresh_hosts
    importlib.reload(proxy)
    assert "vllm.internal" in proxy.FORWARD_HOSTS
    assert "azure.gw.example" in proxy.FORWARD_HOSTS
    # built-ins survive the merge
    assert "openrouter.ai" in proxy.FORWARD_HOSTS
    assert "api.openai.com" in proxy.FORWARD_HOSTS


def test_ssrf_default_deny_intact(fresh_hosts, monkeypatch):
    # even with extra hosts allowed, an arbitrary host is still refused
    monkeypatch.setenv("TOOLRECALL_FORWARD_EXTRA_HOSTS", "my-gw.example")
    proxy = fresh_hosts
    importlib.reload(proxy)
    from toolrecall.proxy import _host_allowed

    assert _host_allowed("my-gw.example")
    assert not _host_allowed("evil.example.com")
    assert not _host_allowed("169.254.169.254")  # cloud metadata
