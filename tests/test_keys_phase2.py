# Copyright (c) 2026 Robin Schultka
# SPDX-License-Identifier: MIT
# Source: https://github.com/whiskybeer/toolrecall
"""Phase-2 consolidation tests: ad-hoc sha256 sites route through keys.

Golden-gate protocol: every expected value here is re-derived from raw
stdlib ``hashlib`` expressions — NOT by replaying the fixture or the
primitive — so a corrupted fixture or a broken primitive cannot
self-confirm. Contract: every migrated site must produce byte-identical
output to the legacy inline expression it replaced.
"""

import hashlib
import random
import unittest

from toolrecall.keys import api_request_hash, content_digest


class TestApiRequestHash(unittest.TestCase):
    """The proxy request-hash derivation, extracted to keys.api_request_hash."""

    # Legacy proxy.py/warp.py derivation, re-derived inline with raw stdlib.
    CASES = [
        (b"{}", "POST", "api.openai.com", "/v1/chat/completions"),
        (b"", "GET", "h", "/p"),
        ('{"emoji":"🚀 — em dash"}'.encode(), "POST", "api.anthropic.com", "/v1/messages"),
        (b"x" * 100000, "POST", "h", "/p"),
    ]

    def test_matches_legacy_derivation(self):
        for body, method, host, path in self.CASES:
            with self.subTest(method=method, host=host, path=path):
                body_hash = hashlib.sha256(body).hexdigest()
                request_str = f"{method}:{host}:{path}:{body_hash}"
                expected = hashlib.sha256(request_str.encode()).hexdigest()
                self.assertEqual(api_request_hash(method, host, path, body), expected)

    def test_width_is_full_sha256(self):
        self.assertEqual(len(api_request_hash("POST", "h", "/p", b"{}")), 64)

    def test_bytes_vs_str_body_equivalence(self):
        # content_digest treats str via surrogatepass UTF-8; the proxy always
        # hashes bytes. Pin that ASCII str/bytes round-trip identically so a
        # future caller passing str (not bytes) doesn't silently diverge.
        self.assertEqual(
            api_request_hash("POST", "h", "/p", b"abc"),
            api_request_hash("POST", "h", "/p", "abc"),
        )


class TestTruncationParity(unittest.TestCase):
    """content_digest(truncate=16) ≡ legacy ``hashlib.sha256(
    s.encode()).hexdigest()[:16]`` — the pattern used by the migrated
    daemon/mcp_bridge/replay sites."""

    TEXTS = [
        "hello",
        "héllo — em dash ünïcode 🚀",
        "",
        "x" * 10000,
    ]

    def test_truncated_parity(self):
        for s in self.TEXTS:
            with self.subTest(repr=s[:20]):
                expected = hashlib.sha256(s.encode()).hexdigest()[:16]
                self.assertEqual(content_digest(s, truncate=16), expected)

    def test_lone_surrogate_is_lossless(self):
        # Documented policy delta: legacy inline sites CRASH on lone
        # surrogates (invalid UTF-8 either way); the primitive encodes
        # losslessly via surrogatepass. Accepted relaxation, pinned here.
        digest = content_digest("a\ud800b", truncate=16)
        self.assertEqual(len(digest), 16)
        self.assertNotEqual(digest, content_digest("a\ufffdb", truncate=16))


class TestProxyWarpParity(unittest.TestCase):
    """warp.make_request_hash must stay byte-identical to
    keys.api_request_hash. The stdlib-only edge cannot import keys
    (deliberate: it deploys without a toolrecall install), so parity is
    enforced HERE instead of by shared code — drift fails the suite."""

    def test_parity_randomized(self):
        from toolrecall.adapters import warp

        rng = random.Random(0xC0FFEE)
        for _ in range(50):
            method = rng.choice(["GET", "POST"])
            host = f"h{rng.randint(0, 9)}"
            path = f"/p{rng.randint(0, 9)}"
            body = bytes(rng.randrange(256) for _ in range(rng.randint(0, 64)))
            self.assertEqual(
                warp.make_request_hash(method, host, path, body),
                api_request_hash(method, host, path, body),
            )

    def test_parity_unicode_body(self):
        from toolrecall.adapters import warp

        body = '{"q":"héllo — ünïcode"}'.encode()
        self.assertEqual(
            warp.make_request_hash("POST", "api.openai.com", "/v1/chat/completions", body),
            api_request_hash("POST", "api.openai.com", "/v1/chat/completions", body),
        )


class TestReplayHashParity(unittest.TestCase):
    """replay._make_hash: only the hash STEP was migrated (normalize_tool_args
    preimage unchanged) — pin the step against a raw-stdlib re-derivation."""

    def test_parity(self):
        from toolrecall import replay
        from toolrecall.normalizer import normalize_tool_args

        cases = [{"query": "x", "limit": 3}, {}, {"a": 1, "b": [2, 1]}]
        for args in cases:
            with self.subTest(args=args):
                args_json = normalize_tool_args(args)
                expected = hashlib.sha256(f"tool:{args_json}".encode()).hexdigest()[:16]
                self.assertEqual(replay._make_hash("tool", args), expected)


if __name__ == "__main__":
    unittest.main()
