"""Regression tests for the CodeQL security fixes (2026-09-08).

Covers three open code-scanning alerts:
- #17 py/polynomial-redos: _strip_ctx_section must be O(n) and byte-identical
  to the old regex sub on all inputs, including adversarial ones.
- #15/#16 py/http-response-splitting: _sanitize_header_pairs strips CR/LF
  from header values and drops headers with unsafe names.
- #1 py/bind-socket-all-network-interfaces: bind_socket refuses wildcard
  hosts at the sink (defense-in-depth on top of _parse_tcp normalization).
"""

import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from toolrecall.proxy import _RE_CTX_SECTION, _canon_text, _strip_ctx_section


class TestCtxStripperEquivalence:
    """_strip_ctx_section must produce the same output as the old regex."""

    CASES = [
        "no markers here",
        "",
        "# Conversation context\n# Next\nafter",
        "before\n# Conversation context junk\n# Next heading\nafter",
        "a\n# Conversation context one\n# Conversation context two\n# x\nend",
        "# Conversation context unterminated till EOF",
        "text\n# Conversation context with\n\nblank lines\n# Next\n",
        "match at start# Conversation context inline",
        "\n# Conversation context\n# \n",
        "multi\n# Conversation context A\n# H1\nmid\n# Conversation context B\n# H2\ntail",
    ]

    @pytest.mark.parametrize("text", CASES)
    def test_identical_to_regex(self, text):
        assert _strip_ctx_section(text) == _RE_CTX_SECTION.sub("", text)

    def test_adversarial_many_markers_unterminated(self):
        # Old regex: O(n²) here. New impl: linear. Outputs must still match.
        text = "\n".join(
            f"# Conversation context run {i} with no following heading" for i in range(500)
        )
        assert _strip_ctx_section(text) == _RE_CTX_SECTION.sub("", text)

    def test_linear_time(self):
        # 20k markers, unterminated — must complete fast (linear), while the
        # old regex on the same input takes minutes (polynomial).
        text = "# Conversation context x\n" * 20000
        t0 = time.monotonic()
        _strip_ctx_section(text)
        elapsed = time.monotonic() - t0
        assert elapsed < 1.0, f"stripper not linear: {elapsed:.2f}s"

    def test_canon_text_end_to_end(self):
        # _canon_text integrates the stripper; CR normalization happens first
        # so "\r\n# " breaks still terminate a section.
        body = "keep\r\n# Conversation context volatile\r\n# Heading\r\ntail"
        out = _canon_text(body)
        assert "volatile" not in out
        assert "keep" in out and "tail" in out


# ── Header sanitization (#15/#16) ────────────────────────────────────────────

from toolrecall.adapters.warp import _sanitize_header_pairs  # noqa: E402


class TestHeaderSanitizer:
    def test_strips_crlf_from_value(self):
        out = dict(_sanitize_header_pairs([("X-Ok", "safe\r\nInjected: evil")]))
        assert out == {"X-Ok": "safeInjected: evil"}

    def test_drops_unsafe_name(self):
        out = dict(_sanitize_header_pairs([("Bad\rName", "v"), ("Good", "v")]))
        assert out == {"Good": "v"}

    def test_clean_headers_untouched(self):
        pairs = [("Content-Type", "application/json"), ("X-Multi", "a b")]
        assert list(_sanitize_header_pairs(pairs)) == pairs

    def test_empty_input(self):
        assert list(_sanitize_header_pairs([])) == []


# ── Bind barrier (#1) ────────────────────────────────────────────────────────

import socket  # noqa: E402

from toolrecall.transport import bind_socket  # noqa: E402


class TestBindBarrier:
    def test_wildcard_ipv4_coerced_to_loopback(self):
        # Public contract: a wildcard path is normalized by _parse_tcp and
        # ends up listening on 127.0.0.1, never on all interfaces.
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            bind_socket(s, "tcp://0.0.0.0:0")
            assert s.getsockname()[0] == "127.0.0.1"
        finally:
            s.close()

    def test_empty_host_coerced_to_loopback(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            bind_socket(s, "tcp://:0")
            assert s.getsockname()[0] == "127.0.0.1"
        finally:
            s.close()

    def test_sink_barrier_raises_if_parse_bypassed(self, monkeypatch):
        # Defense-in-depth: if _parse_tcp ever returned a wildcard host, the
        # sink-level barrier must refuse to bind (fail closed).
        import toolrecall.transport as tr

        monkeypatch.setattr(tr, "_parse_tcp", lambda path: ("0.0.0.0", 0))
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            with pytest.raises(ValueError, match="all interfaces"):
                tr.bind_socket(s, "tcp://0.0.0.0:0")
        finally:
            s.close()

    def test_sink_barrier_raises_on_empty_string_host(self, monkeypatch):
        # The empty string is itself a wildcard bind ('' → all interfaces).
        # CodeQL's query matches its source by literal text and has no
        # sanitizer model, so the barrier uses an inline immutable-literal
        # comparison — exercised here for every wildcard literal form.
        import toolrecall.transport as tr

        for wildcard in ("", "0.0.0.0", "::", "::0"):
            monkeypatch.setattr(tr, "_parse_tcp", lambda path, w=wildcard: (w, 0))
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                with pytest.raises(ValueError, match="all interfaces"):
                    tr.bind_socket(s, "tcp://127.0.0.1:0")
            finally:
                s.close()

    def test_sink_barrier_raises_on_structurally_invalid_host(self, monkeypatch):
        # Anything carrying a scheme, path, or userinfo character can't be a
        # concrete bind address — refuse even though it isn't a wildcard.
        import toolrecall.transport as tr

        for bad in ("evil.com/path", "0.0.0.0 ", "http://x"):
            monkeypatch.setattr(tr, "_parse_tcp", lambda path, b=bad: (b, 0))
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                with pytest.raises(ValueError, match="all interfaces"):
                    tr.bind_socket(s, "tcp://127.0.0.1:0")
            finally:
                s.close()

    def test_loopback_still_binds(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            bind_socket(s, "tcp://127.0.0.1:0")
            assert s.getsockname()[0] == "127.0.0.1"
        finally:
            s.close()

    def test_unix_socket_unaffected(self, tmp_path):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            p = str(tmp_path / "t.sock")
            bind_socket(s, p)
            assert os.path.exists(p)
        finally:
            s.close()
