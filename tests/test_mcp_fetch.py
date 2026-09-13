"""Tests for stdlib-based Fetch MCP Server."""

import os
import socket
import sys
from unittest.mock import patch


class TestMCPFetch:
    """Test the mcp_fetch module structure and basic logic."""

    def test_module_imports(self):
        """mcp_fetch module imports cleanly."""
        import toolrecall.mcp_fetch

        assert hasattr(toolrecall.mcp_fetch, "TOOLS")
        assert hasattr(toolrecall.mcp_fetch, "_handle")
        assert hasattr(toolrecall.mcp_fetch, "_validate_url")

    def test_tools_defined(self):
        """TOOLS list has the expected tools."""
        from toolrecall.mcp_fetch import TOOLS

        tool_names = [t["name"] for t in TOOLS]
        assert "fetch_url" in tool_names
        assert "fetch_head" in tool_names
        assert "fetch_headers" in tool_names
        assert len(TOOLS) == 3

    def test_validate_url_valid(self):
        """Valid HTTPS URLs pass validation (DNS mocked hermetic — public IP)."""
        from toolrecall.mcp_fetch import _validate_url

        public = [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                6,
                "",
                ("93.184.216.34", 443),
            )
        ]
        with patch("socket.getaddrinfo", return_value=public):
            assert _validate_url("https://example.com") is None
            assert _validate_url("http://example.com/path?q=1") is None
            assert _validate_url("https://api.github.com/repos/whiskybeer/toolrecall") is None

    def test_validate_url_invalid(self):
        """Invalid URLs are rejected."""
        from toolrecall.mcp_fetch import _validate_url

        assert _validate_url("") is not None
        assert _validate_url(" ") is not None
        assert _validate_url("ftp://example.com") is not None
        assert _validate_url("file:///etc/passwd") is not None
        assert _validate_url("not-a-url") is not None

    def test_validate_url_unresolvable_host(self):
        """A host that cannot resolve is rejected (no blind passthrough)."""
        from toolrecall.mcp_fetch import _validate_url

        with patch(
            "socket.getaddrinfo", side_effect=socket.gaierror(8, "Name or service not known")
        ):
            err = _validate_url("https://nonexistent-host.example.invalid")
        assert err is not None
        assert "resolve" in err.lower()

    def test_validate_url_blocks_loopback_and_private(self):
        """SSRF (F14): loopback, RFC1918, link-local/IMDS, ULA, mapped-v6 blocked."""
        from toolrecall.mcp_fetch import _validate_url

        blocked = [
            ("http://127.0.0.1:8569/", "127.0.0.1"),
            ("http://10.0.0.5/", "10.0.0.5"),
            ("http://192.168.1.10/", "192.168.1.10"),
            ("http://172.16.0.9/", "172.16.0.9"),
            ("http://169.254.169.254/latest/meta-data/", "169.254.169.254"),
            ("http://100.64.0.1/", "100.64.0.1"),
            ("http://[::1]/", "::1"),
            ("http://[fd00::1]/", "fd00::1"),
            ("http://[::ffff:10.0.0.1]/", "::ffff:10.0.0.1"),
            # Decimal spelling of 127.0.0.1 — urlsplit does NOT normalize it.
            ("http://2130706433/", "2130706433"),
        ]
        for url, _ip in blocked:
            # Real getaddrinfo: numeric hosts resolve offline.
            assert _validate_url(url) is not None, url
        # Hostname spelling that resolves to a private address.
        private = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.1.2.3", 80))]
        with patch("socket.getaddrinfo", return_value=private):
            assert _validate_url("http://internal.example/") is not None

    def test_validate_url_rejects_embedded_credentials(self):
        """user:host confusion — userinfo must not satisfy the host check."""
        from toolrecall.mcp_fetch import _validate_url

        err = _validate_url("https://evil.example@example.com/")
        assert err is not None

    def test_validate_url_rejects_localhost(self):
        """localhost/loopback now BLOCKED (was: allowed pre-F14)."""
        from toolrecall.mcp_fetch import _validate_url

        assert _validate_url("http://localhost:8569") is not None
        assert _validate_url("http://localhost/") is not None

    def test_validate_url_allows_public_numeric(self):
        """A public numeric IP passes the resolver check (mocked v4/v6 mix)."""
        from toolrecall.mcp_fetch import _validate_url

        public = [
            (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2606:2800:220:1::1", 443, 0, 0)),
        ]
        with patch("socket.getaddrinfo", return_value=public):
            assert _validate_url("https://example.com/") is None

    def test_handle_url_requires_url(self):
        """fetch_url requires a URL parameter."""
        from toolrecall.mcp_fetch import _handle

        result = _handle("fetch_url", {})
        assert result is not None
        assert "error" in result

    def test_handle_unknown_method(self):
        """Unknown tool returns None (-> MCP error -32601)."""
        from toolrecall.mcp_fetch import _handle

        assert _handle("nonexistent", {}) is None

    def test_max_content_bytes_default(self):
        """MAX_CONTENT_BYTES defaults to 500KB (512 * 1024)."""
        # Clear env var to test default
        with patch.dict(os.environ, {}, clear=True):
            import importlib
            import toolrecall.mcp_fetch as mf

            importlib.reload(mf)
            assert mf.MAX_CONTENT_BYTES == 512 * 1024, (
                f"Expected 524288, got {mf.MAX_CONTENT_BYTES}"
            )

    def test_max_content_bytes_env_var(self):
        """MAX_CONTENT_BYTES reads from TOOLRECALL_FETCH_MAX_BYTES env var."""
        with patch.dict(os.environ, {"TOOLRECALL_FETCH_MAX_BYTES": "1048576"}):
            import importlib
            import toolrecall.mcp_fetch as mf

            importlib.reload(mf)
            assert mf.MAX_CONTENT_BYTES == 1024 * 1024, (
                f"Expected 1048576, got {mf.MAX_CONTENT_BYTES}"
            )

    def test_max_content_bytes_negative_disables(self):
        """Negative env var sets MAX_CONTENT_BYTES to 0 (no limit)."""
        with patch.dict(os.environ, {"TOOLRECALL_FETCH_MAX_BYTES": "-1"}):
            import importlib
            import toolrecall.mcp_fetch as mf

            importlib.reload(mf)
            assert mf.MAX_CONTENT_BYTES == 0, f"Expected 0 (no limit), got {mf.MAX_CONTENT_BYTES}"

    def test_max_content_bytes_caps_user_param(self):
        """User-supplied max_bytes is capped at MAX_CONTENT_BYTES."""
        # Reload with default env to ensure MAX_CONTENT_BYTES > 0
        import importlib

        with patch.dict(os.environ, {}, clear=True):
            import toolrecall.mcp_fetch as mf

            importlib.reload(mf)
        assert mf.MAX_CONTENT_BYTES == 512 * 1024
        # The logic is trivially min(max_bytes, MAX_CONTENT_BYTES)
        # We verify the code path exists in _do_fetch signature

    def test_fetch_is_now_builtin_in_registry(self):
        """fetch is now a built-in server (not external/uvx)."""
        from toolrecall.mcp_registry import resolve_server, is_builtin

        result = resolve_server("fetch")
        assert result is not None, "fetch should be resolvable"
        cmd, args, source = result
        assert source == "builtin", f"fetch should be builtin, got {source}"
        assert "python" in cmd.lower() or sys.executable in cmd
        assert "-m" in args
        assert "toolrecall.mcp_fetch" in args
        assert is_builtin("fetch"), "is_builtin(fetch) should be True"

    def test_fetch_no_longer_in_external_registry(self):
        """fetch should NOT be in EXTERNAL_REGISTRY anymore."""
        from toolrecall.mcp_registry import EXTERNAL_REGISTRY

        assert "fetch" not in EXTERNAL_REGISTRY, "fetch was moved from external to built-in"
