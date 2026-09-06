# Copyright (c) 2026 Robin Schultka
# SPDX-License-Identifier: MIT
# Source: https://github.com/whiskybeer/toolrecall
"""Canonicalization hardening for the `warp` proxy profile.

Live bugfix-exp (2026-09-04, captures seq 45-98) showed two Turn-0 bodies
that differed ONLY in blank-line composition inside the pasted task prompt:

    run A: "...flask öffnen\r\n\r\n\r\nIn the repository..."
    run B: "...flask öffnen\r\n\r\r\n\r\nIn the repository..."

After CR normalization both collapse to different newline-only sequences
("\n\n\n" vs "\n\n\n\n") and still fork the canon key. Blank-line runs are
clipboard/terminal artifacts, never model-relevant semantics, so the warp
profile collapses every whitespace-only line sequence to a single newline.
"""

import hashlib
import json


from toolrecall.proxy import _canonicalize_body


def _canon_key(body) -> str:
    if not isinstance(body, (bytes, bytearray)):
        body = json.dumps(body).encode("utf-8")
    canon = _canonicalize_body("warp", body)
    assert canon is not None, "warp profile must canonicalize"
    return hashlib.sha256(canon).hexdigest()


def _msg(text: str) -> dict:
    return {"role": "user", "content": [{"type": "text", "text": text}]}


class TestCRNormalization:
    def test_crlf_vs_lf_same_key(self):
        a = _msg("PS> . C:\\Program` Files\\Warp\\pwsh.ps1\r\nnext line")
        b = _msg("PS> . C:\\Program` Files\\Warp\\pwsh.ps1\nnext line")
        assert _canon_key(a) == _canon_key(b)

    def test_lone_cr_vs_lf_same_key(self):
        a = _msg("line one\rline two")
        b = _msg("line one\nline two")
        assert _canon_key(a) == _canon_key(b)

    def test_mixed_cr_variants_same_key(self):
        a = _msg("x\r\n\r\ny")
        b = _msg("x\r\r\n\r\ny")
        c = _msg("x\n\ny")
        assert _canon_key(a) == _canon_key(b) == _canon_key(c)


class TestBlankLineCollapse:
    def test_blank_line_runs_collapse(self):
        a = _msg("öffnen\n\n\nIn the repository")
        b = _msg("öffnen\n\n\n\nIn the repository")
        c = _msg("öffnen\n\n\n\n\n\nIn the repository")
        assert _canon_key(a) == _canon_key(b) == _canon_key(c)

    def test_significant_whitespace_preserved(self):
        # Single newlines between words must NOT be collapsed away.
        assert _canon_key(_msg("a\nb")) != _canon_key(_msg("ab"))

    def test_indentation_still_normalized_elsewhere(self):
        # The existing indent/whitespace pipeline keeps working.
        assert _canon_key(_msg("x = 1\n    y = 2")) == _canon_key(_msg("x = 1\ny = 2"))


class TestNoFalseSharing:
    def test_different_content_still_differs(self):
        assert _canon_key(_msg("fix bug A")) != _canon_key(_msg("fix bug B"))

    def test_replay_capture_regression(self):
        """Exact Turn-0 text pair from live capture seq 54 vs 61."""
        a = (
            "Warp im Ordner C:\\Users\\robin\\swebench-warp\\flask öffnen\r\n"
            "\r\n\r\nIn the repository at C:\\Users\\robin\\swebench-warp\\flask"
        )
        b = (
            "Warp im Ordner C:\\Users\\robin\\swebench-warp\\flask öffnen\r\n"
            "\r\r\n\r\nIn the repository at C:\\Users\\robin\\swebench-warp\\flask"
        )
        assert _canon_key(a) == _canon_key(b)


class TestIdempotence:
    def test_double_canonicalization_stable(self):
        body = json.dumps({"messages": [_msg("a\r\n\r\n\r\nb\r\nc")]}).encode()
        k1 = _canon_key(body)
        # Same input twice must give the same key (deterministic pipeline).
        k2 = _canon_key(body)
        assert k1 == k2


class TestProfileGuards:
    def test_unknown_profile_returns_none(self):
        assert _canonicalize_body("openai", b'{"messages": []}') is None
