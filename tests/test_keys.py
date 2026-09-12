# Copyright (c) 2026 Robin Schultka
# SPDX-License-Identifier: MIT
# Source: https://github.com/whiskybeer/toolrecall
"""Stage-0 primitive tests: canonical key derivation (toolrecall.keys).

Two layers:
1. Golden vectors (tests/golden_keys.json) — captured from the
   pre-consolidation adapter code. Pins byte-compat: any change to key
   or preimage bytes must fail here loudly, never silently migrate keys.
2. Hypothesis properties — the primitive contract: purity, order
   invariance, injectivity, tool-name binding.
"""

import hashlib
import json
import os
import unittest

try:
    from hypothesis import HealthCheck, given, settings
    from hypothesis import strategies as st

    _HAVE_HYPOTHESIS = True
except ImportError:  # pragma: no cover
    _HAVE_HYPOTHESIS = False

from toolrecall.keys import _normalize_args, content_digest, digest16, tool_key

_GOLDEN_PATH = os.path.join(os.path.dirname(__file__), "golden_keys.json")

# Fresh golden vectors (not from the file) — derived from raw stdlib so a
# corrupted goldens file cannot self-confirm a broken implementation.
_FRESH_TEXTS = ["hello", "héllo — em dash ünïcode", "x" * 5000, "a\ud800b"]

_FRESH_CASES = [
    ((), {}),
    (("grep", "-n", "def main"), {"cwd": "/x"}),
    (
        (),
        {
            "query": "héllo wörld — ünïcode",
            "limit": 10,
            "flag": True,
            "nothing": None,
            "ratio": 0.5,
            "ñkey": True,
        },
    ),
    ((42, 3.14), {}),
    ((), {"opts": {"b": 2, "a": 1}}),
    (("a",), {"b": ["z", "a"], "c": {"k": [1, {"y": 1, "x": 2}]}}),
    (("",), {"empty": "", "zero": 0, "f": False}),
]


def _legacy_preimage(tool: str, args: tuple, kwargs: dict) -> str:
    """The pre-Stage-0 odysseus._build_cache_key, verbatim."""
    arg_dict = {}
    if args:
        arg_dict["_pos"] = [str(a) for a in args]
    for k, v in kwargs.items():
        if isinstance(v, (str, int, float, bool, type(None))):
            arg_dict[k] = v
        else:
            arg_dict[k] = str(v)
    return json.dumps(arg_dict, sort_keys=True, ensure_ascii=False)


class TestContentDigestGolden(unittest.TestCase):
    """content_digest must reproduce pre-Stage-0 byte values."""

    def test_fresh_from_stdlib(self):
        """surrogatepass policy pinned by independent derivation."""
        for text in _FRESH_TEXTS:
            with self.subTest(text=text[:20]):
                self.assertEqual(
                    content_digest(text),
                    hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest(),
                )

    def test_bytes_pass_through(self):
        payload = b"\x89PNG\r\n\x1a\n"
        self.assertEqual(content_digest(payload), hashlib.sha256(payload).hexdigest())
        self.assertEqual(content_digest(bytearray(payload)), content_digest(payload))

    def test_golden_file_bridge_vectors(self):
        """Legacy mcp_bridge 'replace' digests reproduced for all valid text."""
        with open(_GOLDEN_PATH, encoding="utf-8") as fh:
            golden = json.load(fh)
        for text, want in zip(golden["texts"], golden["bridge_digest64_replace"]):
            with self.subTest(text=text[:20]):
                if "\ud800" in text:
                    continue  # documented policy delta: lone surrogates → surrogatepass
                self.assertEqual(content_digest(text), want)

    def test_golden_file_litellm_vectors(self):
        """Legacy litellm 16-hex stub digests reproduced exactly."""
        with open(_GOLDEN_PATH, encoding="utf-8") as fh:
            golden = json.load(fh)
        for text, want in zip(golden["texts"], golden["litellm_digest16"]):
            with self.subTest(text=text[:20]):
                self.assertEqual(digest16(text), want)
                self.assertEqual(content_digest(text, truncate=16), want)

    def test_truncate_matches_legacy_slice(self):
        for text in _FRESH_TEXTS:
            self.assertEqual(
                digest16(text),
                hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()[:16],
            )

    def test_truncate_bounds(self):
        full = content_digest("abc")
        for n in (8, 16, 64):
            self.assertEqual(content_digest("abc", truncate=n), full[:n])
        for bad in (0, 7, 65, -1):
            with self.assertRaises(ValueError):
                content_digest("abc", truncate=bad)

    def test_rejects_non_byteslike(self):
        for bad in (None, 42, {"a": 1}, [1, 2]):
            with self.assertRaises(TypeError):
                content_digest(bad)


class TestToolKeyGolden(unittest.TestCase):
    """Preimage path must be byte-identical to legacy odysseus keys."""

    def test_golden_file_preimages(self):
        with open(_GOLDEN_PATH, encoding="utf-8") as fh:
            golden = json.load(fh)
        for case, want in zip(golden["cases"], golden["legacy_preimages"]):
            with self.subTest(case=case):
                got = tool_key("test_tool", tuple(case["args"]), case["kwargs"], digest=False)
                self.assertEqual(got, want)

    def test_fresh_preimage_matches_legacy_function(self):
        for args, kwargs in _FRESH_CASES:
            with self.subTest(args=args, kwargs=kwargs):
                self.assertEqual(
                    tool_key("t", args, kwargs, digest=False),
                    _legacy_preimage("t", args, kwargs),
                )


# Module-level strategies: lambdas inside st.recursive cannot close over
# class-body names (class scope is not a closure scope).
_KEYS = st.text(min_size=1, max_size=12)
_JSON_VALUES = st.recursive(
    st.none()
    | st.booleans()
    | st.integers(min_value=-(10**9), max_value=10**9)
    | st.floats(allow_nan=False, allow_infinity=False)
    | st.text(max_size=24),
    lambda children: st.lists(children, max_size=4) | st.dictionaries(_KEYS, children, max_size=4),
    max_leaves=8,
)
_ARG_DICTS = st.dictionaries(_KEYS, _JSON_VALUES, max_size=6)


@unittest.skipUnless(_HAVE_HYPOTHESIS, "hypothesis not installed")
class TestToolKeyProperties(unittest.TestCase):
    """The primitive contract — order invariance, injectivity, binding."""

    @settings(suppress_health_check=[HealthCheck.too_slow], max_examples=150)
    @given(arg_dicts=_ARG_DICTS)
    def test_kwarg_insertion_order_invariance(self, arg_dicts):
        """Permuting dict insertion order must not change any key byte."""
        base = tool_key("t", (), arg_dicts)
        for _ in range(3):
            shuffled = list(arg_dicts.items())
            shuffled.reverse()
            self.assertEqual(tool_key("t", (), dict(shuffled)), base)
            self.assertEqual(
                tool_key("t", (), dict(shuffled), digest=False),
                _legacy_preimage("t", (), dict(shuffled)),
            )

    @settings(suppress_health_check=[HealthCheck.too_slow], max_examples=150)
    @given(arg_dicts=_ARG_DICTS)
    def test_any_arg_difference_changes_digest(self, arg_dicts):
        """Mutating any value must change the bound digest (collision-free
        by sha256 assumption, verified structurally here)."""
        base = tool_key("t", (), arg_dicts)
        if not arg_dicts:
            return
        k = next(iter(arg_dicts))
        mutated = dict(arg_dicts)
        mutated[k] = object()  # non-JSON-safe → str-coerced, differs from any JSON value
        self.assertNotEqual(tool_key("t", (), mutated), base)

    @settings(suppress_health_check=[HealthCheck.too_slow], max_examples=100)
    @given(t1=_KEYS, t2=_KEYS, a1=_ARG_DICTS, a2=_ARG_DICTS)
    def test_tool_name_binds_into_digest(self, t1, t2, a1, a2):
        """Digest path binds the tool name: same args, different tool →
        different key. This is the collision bug Stage-0 was written to
        prevent; if it ever fires, keys collide across tools."""
        k1 = tool_key(t1, (), a1)
        k2 = tool_key(t2, (), a1)
        if t1 != t2:
            self.assertNotEqual(k1, k2)
        # preimage path is intentionally tool-free (legacy namespacing)
        self.assertEqual(
            tool_key(t1, (), a1, digest=False),
            tool_key(t2, (), a1, digest=False),
        )

    @settings(max_examples=100)
    @given(args=st.lists(st.text(max_size=20), max_size=4))
    def test_positional_args_normalized_as_strings(self, args):
        """Positional args coerce to str under _pos (legacy contract)."""
        pre = _normalize_args(tuple(args), {})
        if args:
            parsed = json.loads(pre)
            self.assertEqual(parsed["_pos"], [str(a) for a in args])
        else:
            self.assertNotIn("_pos", json.loads(pre))


class TestPurityContract(unittest.TestCase):
    """The primitive must stay pure — same input, same output, no state."""

    def test_repeat_calls_identical(self):
        for _ in range(3):
            self.assertEqual(content_digest("stable"), content_digest("stable"))
            self.assertEqual(tool_key("t", ("a",), {"b": 1}), tool_key("t", ("a",), {"b": 1}))

    def test_digest_path_matches_manual_stdlib_derivation(self):
        """The bound digest is derivable with nothing but stdlib — that is
        the standalone-import guarantee for external consumers."""
        pre = _legacy_preimage("mytool", ("x",), {"y": 2})
        manual = hashlib.sha256(
            json.dumps({"args": pre, "tool": "mytool"}, sort_keys=True, ensure_ascii=False).encode(
                "utf-8", "surrogatepass"
            )
        ).hexdigest()
        self.assertEqual(tool_key("mytool", ("x",), {"y": 2}), manual)


if __name__ == "__main__":
    unittest.main()
