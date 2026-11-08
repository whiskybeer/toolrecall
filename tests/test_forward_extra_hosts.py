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
    # Set-subset comparison (NOT `x in FORWARD_HOSTS` string containment):
    # a `"<host>.<tld>" in <collection>` compare trips CodeQL's
    # py/incomplete-url-substring-sanitization syntax heuristic — membership
    # in a set of exact hosts is the complete sanitization it wants.
    expected = {"vllm.internal", "azure.gw.example", "openrouter.ai", "api.openai.com"}
    assert expected.issubset(proxy.FORWARD_HOSTS)


def test_ssrf_default_deny_intact(fresh_hosts, monkeypatch):
    # even with extra hosts allowed, an arbitrary host is still refused
    monkeypatch.setenv("TOOLRECALL_FORWARD_EXTRA_HOSTS", "my-gw.example")
    proxy = fresh_hosts
    importlib.reload(proxy)
    from toolrecall.proxy import _host_allowed

    assert _host_allowed("my-gw.example")
    assert not _host_allowed("evil.example.com")
    assert not _host_allowed("169.254.169.254")  # cloud metadata


class TestSSRFSinkBarrier:
    """Sink-level structural barrier (CodeQL py/full-ssrf).

    _host_allowed() is invisible to CodeQL's dataflow; the sink barrier is a
    re.fullmatch against _SSRF_SAFE_HOST — a shape the query models as a
    sanitizer. These tests pin its contract: structurally valid host tokens
    pass (runtime authorization is _host_allowed's job), anything that could
    carry a scheme, userinfo, or path is rejected fail-closed.
    """

    @pytest.mark.parametrize(
        "host",
        [
            "api.openai.com",
            "openrouter.ai:443",
            "localhost",
            "127.0.0.1",
            "::1",
            "[::1]:8080",
            "vllm.internal",  # bare extra_hosts name, no dot
            "2001:db8::1",  # bare IPv6 extra_hosts
            "AZURE.GW.example:8443",  # caller case is preserved (checked lower elsewhere)
        ],
    )
    def test_structurally_valid_hosts_pass(self, host):
        from toolrecall.proxy import _SSRF_SAFE_HOST

        assert _SSRF_SAFE_HOST.fullmatch(host)

    @pytest.mark.parametrize(
        "host",
        [
            "",
            "http://evil.com",
            "api.openai.com/../../admin",
            "user@169.254.169.254",
            "api.openai.com?x=1",
            "api.openai.com:8080/path",
            " 127.0.0.1",
            "127.0.0.1\n",  # CR/LF injection attempt
        ],
    )
    def test_structurally_invalid_hosts_rejected(self, host):
        from toolrecall.proxy import _SSRF_SAFE_HOST

        assert not _SSRF_SAFE_HOST.fullmatch(host)

    def test_sink_barrier_blocks_when_helper_bypassed(self):
        """If _host_allowed were bypassed, the sink barrier still refuses.

        Uses a structurally INVALID host (carries a path) so the barrier —
        not the network layer — produces the 403, with the helper mocked
        open to prove the barrier alone is the guard.
        """
        import unittest.mock as mock

        import toolrecall.proxy as proxy

        # No __init__: the barrier returns before any instance state is used.
        handler = object.__new__(proxy.ForwardProxyHandler)
        with mock.patch.object(proxy, "_host_allowed", return_value=True):
            status, _headers, body = proxy.ForwardProxyHandler._forward_once(
                handler,
                "POST",
                "169.254.169.254/evil",  # structurally invalid → barrier 403
                "/v1/chat/completions",
                "https",
                b"{}",
            )
        assert status == 403
        assert b"non-allowlisted" in body

    def test_streaming_barrier_shape_matches_sink_barrier(self):
        """The streaming sink uses the same structural check — pin the edge
        cases: metadata IPs are _host_allowed's job (structurally ordinary
        IPv4); path/scheme contamination is the barrier's job."""
        from toolrecall.proxy import _SSRF_SAFE_HOST

        assert _SSRF_SAFE_HOST.fullmatch("169.254.169.254")  # structural only
        assert not _SSRF_SAFE_HOST.fullmatch("metadata.google.internal/")
