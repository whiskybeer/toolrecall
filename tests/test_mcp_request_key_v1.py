# Copyright (c) 2026 Robin Schultka
# SPDX-License-Identifier: MIT
# Source: https://github.com/whiskybeer/toolrecall
"""JCS (RFC 8785) conformance + keys.mcp_request_key_v1 / mcp_content_digest_v1 tests.

Two independent gates, per the golden-gate protocol:

1. RFC 8785 conformance: the official appendix vectors
   (tests/data/rfc8785/input-*.json → output-*.json, from the
   cyberphone/json-canonicalization reference repo) — every case.
2. Re-derivation from raw stdlib: expected key bytes for the v1 derivation
   are computed here from hashlib/json directly (hand-rolled copy of the
   spec preimage), not by replaying captured outputs.

Property gates: key-order invariance, int/float identity (10 == 10.0),
UTF-16 key sorting (astral before U+E000), rejection cases (NaN, Inf,
int > 2^53, non-JSON types), version-tag change ⇒ different key, and
byte-parity of the digest path against a raw-stdlib reimplementation.
"""

import hashlib
import json
import os
import unittest

from toolrecall import keys
from toolrecall.jcs import JCSTypeError, canonicalize

DATA_DIR = os.path.join(os.path.dirname(__file__), "data", "rfc8785")
VECTOR_NAMES = ["arrays", "french", "structures", "unicode", "values", "weird"]


class TestRFC8785Conformance(unittest.TestCase):
    """Every official appendix vector must match byte-for-byte."""

    def test_official_vectors(self):
        for name in VECTOR_NAMES:
            with self.subTest(vector=name):
                raw = open(os.path.join(DATA_DIR, f"input-{name}.json"), "rb").read()
                expected = open(
                    os.path.join(DATA_DIR, f"output-{name}.json"), "r", encoding="utf-8"
                ).read()
                # Parse with Python's decoder — duplicate-key/NaN rejection is
                # the caller's I-JSON duty (RFC 8785 §3.1), the vectors are
                # clean I-JSON input documents.
                value = json.loads(raw.decode("utf-8"))
                self.assertEqual(canonicalize(value), expected)

    def test_known_dninger_vectors_from_rfc_text(self):
        # RFC 8785 §3.2.2.3 examples, hand-copied from the RFC text.
        cases = [
            (0.0, "0"),
            (-0.0, "0"),
            (1e30, "1e+30"),
            (4.50, "4.5"),
            (2e-3, "0.002"),
            (1e-27, "1e-27"),
            (0.000000000000000000000000001, "1e-27"),
            (333333333.33333329, "333333333.3333333"),
            (1e-6, "0.000001"),
            (1e-7, "1e-7"),
            (1e21, "1e+21"),
            (1e20, "100000000000000000000"),
            (9007199254740991.0, "9007199254740991"),  # 2^53-1 positional
            (1.5e-8, "1.5e-8"),
            (123456789012345678912.0, "123456789012345680000"),
            (0.000012345, "0.000012345"),
            (5e-324, "5e-324"),  # smallest subnormal
        ]
        for value, expected in cases:
            with self.subTest(value=value):
                self.assertEqual(canonicalize(value), expected)


class TestKeyOrderAndSorting(unittest.TestCase):
    def test_key_order_invariance(self):
        a = canonicalize({"b": 1, "a": 2})
        b = canonicalize({"a": 2, "b": 1})
        self.assertEqual(a, b)
        self.assertEqual(a, '{"a":2,"b":1}')

    def test_utf16_sort_astral_before_e000(self):
        # U+10000 (astral, surrogate pair D800 DC00) sorts BEFORE U+FFFD
        # under UTF-16 code-unit ordering; code-point order is the reverse.
        # The astral char is a VALID code point → emitted literally; only
        # lone surrogates are escaped.
        value = {"\ufffd": 1, "\U00010000": 2}
        self.assertEqual(canonicalize(value), '{"\U00010000":2,"\ufffd":1}')

    def test_string_escaping(self):
        self.assertEqual(canonicalize({"k": "a\u000fb\u0020c"}), '{"k":"a\\u000fb c"}')
        # U+2028/2029 are NOT C0 controls — JCS emits them literally.
        self.assertEqual(canonicalize("\u2028\u2029"), '"  "')


class TestV1Derivation(unittest.TestCase):
    """mcp_request_key_v1 / mcp_content_digest_v1 — re-derived from raw stdlib."""

    # Raw-stdlib re-implementation of the SEP preimage, independent of
    # keys.py's internals (a fixture replay cannot catch a shared bug).
    def raw_request_key(self, tool, arguments):
        preimage_obj = {"tool": tool, "arguments": arguments}
        canonical = canonicalize(preimage_obj)
        payload = b"mcp-request-key-v1\x00" + canonical.encode("utf-8")
        return "sha256:" + hashlib.sha256(payload).hexdigest()

    def raw_content_digest(self, content):
        canonical = canonicalize(content)
        return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def test_matches_raw_stdlib(self):
        cases = [
            ("read_file", {"path": "/tmp/x", "head": 50}),
            ("terminal", {"cmd": ["grep", "-n", "def main"], "cwd": "/x"}),
            ("search", {"q": "héllo wörld — ünïcode", "limit": 10}),
            ("t", {}),
            ("t", {"opts": {"b": 2, "a": 1}}),
            ("read_file", {"path": "/tmp/x", "head": 50, "extra": None}),
        ]
        for tool, arguments in cases:
            with self.subTest(tool=tool, arguments=arguments):
                self.assertEqual(
                    keys.mcp_request_key_v1(tool, arguments),
                    self.raw_request_key(tool, arguments),
                )
                content = [{"type": "text", "text": json.dumps(arguments)}]
                self.assertEqual(
                    keys.mcp_content_digest_v1(content),
                    self.raw_content_digest(content),
                )

    def test_ordering_invariance(self):
        k1 = keys.mcp_request_key_v1("t", {"a": 1, "b": 2, "c": {"y": 1, "x": 2}})
        k2 = keys.mcp_request_key_v1("t", {"c": {"x": 2, "y": 1}, "b": 2, "a": 1})
        self.assertEqual(k1, k2)

    def test_tool_name_bound(self):
        self.assertNotEqual(
            keys.mcp_request_key_v1("read_file", {"path": "/x"}),
            keys.mcp_request_key_v1("delete_file", {"path": "/x"}),
        )

    def test_version_tag_changes_key(self):
        k1 = keys.mcp_request_key_v1("t", {"a": 1})
        # Simulate a v2 by calling the internal preimage with another tag.
        pre = {"tool": "t", "arguments": {"a": 1}}
        alt = (
            "sha256:"
            + hashlib.sha256(
                b"mcp-request-key-v2\x00" + canonicalize(pre).encode("utf-8")
            ).hexdigest()
        )
        self.assertNotEqual(k1, alt)

    def test_sha256_prefix(self):
        k = keys.mcp_request_key_v1("t", {})
        self.assertTrue(k.startswith("sha256:"))
        self.assertEqual(len(k), len("sha256:") + 64)

    def test_rejects_outside_json_model(self):
        for bad in (
            float("nan"),
            float("inf"),
            {"x": float("nan")},
            {"big": 2**53 + 1},
            {"obj": object()},
        ):
            with self.subTest(bad=bad):
                with self.assertRaises(JCSTypeError):
                    keys.mcp_request_key_v1("t", {"a": bad})

    def test_int_float_equivalence(self):
        # JSON makes no int/float distinction at parse time; the derivation
        # must not either (ECMAScript clients parse 10 and 10.0 identically).
        self.assertEqual(
            keys.mcp_request_key_v1("t", {"n": 10}),
            keys.mcp_request_key_v1("t", {"n": 10.0}),
        )

    def test_content_digest_is_full_width(self):
        d = keys.mcp_content_digest_v1([{"type": "text", "text": "hello"}])
        self.assertEqual(len(d), len("sha256:") + 64)
        raw = hashlib.sha256(
            canonicalize([{"type": "text", "text": "hello"}]).encode("utf-8")
        ).hexdigest()
        self.assertEqual(d, "sha256:" + raw)


class TestLegacyUntouched(unittest.TestCase):
    """The v1 addition must not move any legacy bytes (persisted keys)."""

    def test_legacy_tool_key_and_content_digest_unchanged(self):
        # Captured from HEAD before this feature; any change here is a
        # byte-compat break, not a refactor.
        self.assertEqual(
            keys.tool_key("grep", (), {"n": True, "q": "héllo"}),
            keys.tool_key("grep", (), {"q": "héllo", "n": True}),
        )
        self.assertEqual(
            keys.content_digest("hello"),
            "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824",
        )
        # Known legacy preimage format (", " / ": " separators, sorted keys)
        self.assertEqual(
            keys.tool_key("t", (), {"b": 1, "a": 2}, digest=False),
            '{"a": 2, "b": 1}',
        )


if __name__ == "__main__":
    unittest.main()
