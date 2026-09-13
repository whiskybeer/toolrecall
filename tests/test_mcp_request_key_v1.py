# Copyright (c) 2026 Robin Schultka
# SPDX-License-Identifier: MIT
# Source: https://github.com/whiskybeer/toolrecall
"""JCS (RFC 8785) conformance + keys.mcp_request_key_v1 / mcp_content_digest_v1 tests.

Two independent gates, per the golden-gate protocol:

1. RFC 8785 conformance: the official appendix vectors
   (tests/data/rfc8785/input-*.json → output-*.json, from the
   cyberphone/json-canonicalization reference repo) — every case.
2. Re-derivation from raw stdlib: expected key bytes for the v1 derivation
   are computed here from hashlib directly (hand-rolled copy of the spec
   preimages), not by replaying captured outputs. The contentDigest
   preimage is version-tagged (mcp-content-digest-v1 ∥ 0x00 ∥ JCS(content))
   — symmetric with requestKey, so derivation variants can never collide
   as identical "sha256:<64hex>" strings.

3. End-to-end golden vectors (tests/golden_mcp_v1.json): (tool, arguments)
   → requestKey and content → contentDigest for representative payloads,
   consumable by another language's implementer without running Python.
   Generated alongside the reference implementation; regeneration is a
   version-tag bump, never a silent overwrite.

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
        # The astral char is a VALID code point → emitted literally; lone
        # surrogates are rejected outright (see TestSurrogatePolicy).
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
        payload = b"mcp-content-digest-v1\x00" + canonical.encode("utf-8")
        return "sha256:" + hashlib.sha256(payload).hexdigest()

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
            b"mcp-content-digest-v1\x00"
            + canonicalize([{"type": "text", "text": "hello"}]).encode("utf-8")
        ).hexdigest()
        self.assertEqual(d, "sha256:" + raw)


class TestSurrogatePolicy(unittest.TestCase):
    """Lone surrogates are rejected UNIFORMLY — keys and values, any size.

    Python's json.loads happily produces them from escapes like "\\ud800",
    so a hostile tool payload can carry them; pre-rejection the failure was
    size-dependent ({single surrogate key} passed, two keys crashed with
    UnicodeEncodeError in the UTF-16 sort) and surrogate values were
    escaped ES-style — bytes no other RFC 8785 implementation reproduces.
    The contract now: JCSTypeError everywhere, never UnicodeEncodeError /
    TypeError / AttributeError leaking to a caller that catches only
    JCSTypeError.
    """

    def test_surrogate_key_rejected_single_and_multi(self):
        for value in ({"\ud800": 1}, {"\ud800": 1, "a": 2}, {"a": 2, "\udbff": 1}):
            with self.subTest(value=repr(value)):
                with self.assertRaises(JCSTypeError):
                    canonicalize(value)

    def test_surrogate_value_rejected_anywhere(self):
        for value in (
            {"a": "\ud800"},
            ["\udbff\udfff"],
            {"deep": {"nested": ["\ud800"]}},
            "\ud800",  # top-level string
        ):
            with self.subTest(value=repr(value)):
                with self.assertRaises(JCSTypeError):
                    canonicalize(value)

    def test_v1_functions_raise_jcs_type_error_only(self):
        # The remotely-reachable case: a hostile payload's lone-surrogate
        # key must surface as the documented exception type, not a leak.
        for fn, args in (
            (keys.mcp_request_key_v1, ("t", {"\ud800": 1, "a": 2})),
            (keys.mcp_request_key_v1, ("t", {"a": "\ud800"})),
            (keys.mcp_content_digest_v1, ({"\ud800": 1},)),
            (keys.mcp_content_digest_v1, ("\udbff\udfff",)),
        ):
            with self.subTest(fn=fn.__name__, args=repr(args)[:40]):
                try:
                    fn(*args)
                except JCSTypeError:
                    pass
                except Exception as e:  # noqa: BLE001 — the leak IS the bug
                    self.fail(f"{fn.__name__} leaked {type(e).__name__}: {e}")
                else:
                    self.fail(f"{fn.__name__} accepted a lone surrogate")

    def test_non_string_keys_rejected_as_jcs_type_error(self):
        for key in (1, None, True, 3.14):
            with self.subTest(key=repr(key)):
                with self.assertRaises(JCSTypeError):
                    canonicalize({key: "v"})

    def test_valid_astral_and_bmp_text_still_canonicalizes(self):
        # Rejection is scoped to LONE surrogates; valid astral code points
        # (surrogate PAIRS in Python strings) and BMP text are untouched.
        self.assertEqual(
            canonicalize({"k": "\U0001f600 é —"}),
            '{"k":"\U0001f600 é —"}',
        )

    def test_digest_preimage_tag_discriminates_variants(self):
        # The open-question case: two derivation variants (tagged JCS vs
        # untagged JCS vs requestKey) must never be confusable. The tag
        # makes them produce visibly different outputs.
        content = [{"type": "text", "text": "hello"}]
        tagged = keys.mcp_content_digest_v1(content)
        untagged = (
            "sha256:" + hashlib.sha256(canonicalize(content).encode("utf-8")).hexdigest()
        )  # the pre-tag derivation — a would-be silent variant
        request_key = keys.mcp_request_key_v1("t", content)
        self.assertNotEqual(tagged, untagged)
        self.assertNotEqual(tagged, request_key)


class TestGoldenVectors(unittest.TestCase):
    """End-to-end fixture: (tool, arguments) → key, content → digest.

    golden_mcp_v1.json is the cross-implementation contract — a Go/Rust/TS
    implementer replays these vectors without running Python. The file is
    frozen: any byte change to the derivations must come with a version-tag
    bump and a regenerated file, never a silent overwrite.
    """

    GOLDEN = os.path.join(os.path.dirname(__file__), "golden_mcp_v1.json")

    @classmethod
    def setUpClass(cls):
        with open(cls.GOLDEN, encoding="utf-8") as fh:
            cls.golden = json.load(fh)

    def test_request_key_vectors(self):
        for case in self.golden["request_keys"]:
            with self.subTest(tool=case["tool"], note=case.get("note", "")):
                self.assertEqual(
                    keys.mcp_request_key_v1(case["tool"], case["arguments"]),
                    case["requestKey"],
                )

    def test_content_digest_vectors(self):
        for case in self.golden["content_digests"]:
            with self.subTest(content=repr(case["content"])[:40], note=case.get("note", "")):
                self.assertEqual(
                    keys.mcp_content_digest_v1(case["content"]),
                    case["contentDigest"],
                )

    def test_payload_json_vectors(self):
        for case in self.golden["rejection_cases"]:
            with self.subTest(note=case["note"]):
                payload = json.loads(case["payload_json"])
                if case["expected"] == "error":
                    with self.assertRaises(JCSTypeError):
                        keys.mcp_request_key_v1("t", payload)
                else:
                    # e.g. an escaped surrogate PAIR is a valid astral char:
                    # must canonicalize, not reject.
                    keys.mcp_request_key_v1("t", payload)

    def test_spec_block_pins_the_preimages(self):
        spec = self.golden["spec"]
        self.assertIn("mcp-request-key-v1", spec["requestKey"])
        self.assertIn("mcp-content-digest-v1", spec["contentDigest"])
        self.assertIn("RFC 8785", spec["canonicalization"])


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
