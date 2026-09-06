"""Property-based tests for pure normalization/serialization modules.

Hypothesis-driven fuzzing of the components most exposed to hostile or
unusual external input:

  * normalizer.normalize_json / normalize_tool_args — cache-key determinism.
    THE critical invariant: semantically identical input must always produce
    an identical key (key-order, whitespace, list-order independence), and
    the output must be deterministic (same input -> same output, always).
  * normalizer.normalize_command — idempotence + arg preservation.
  * path_utils.check_path_allowed — security invariants over generated
    paths: prefix-boundary (no /data-evil under /data), default-deny on
    empty allowlist, exact match and subdirectory allowed.
  * toml_serializer.dumps — round-trip through stdlib tomllib for every
    supported value type (the parser is the oracle).

Run standalone: .venv/bin/python -m pytest tests/test_properties.py
"""

import os
import string
import tempfile
import tomllib
import unittest

from hypothesis import given, settings, strategies as st

from toolrecall.normalizer import normalize_command, normalize_json, normalize_tool_args
from toolrecall.path_utils import check_path_allowed
from toolrecall import toml_serializer

# ── strategies ────────────────────────────────────────────────

json_values = st.recursive(
    st.none()
    | st.booleans()
    | st.integers(min_value=-(2**53), max_value=2**53)
    | st.floats(allow_nan=False, allow_infinity=False)
    | st.text(
        alphabet=string.printable, max_size=40
    ),  # printable incl. whitespace to exercise strip
    lambda children: (
        st.lists(children, max_size=6)
        | st.dictionaries(
            st.text(alphabet=string.ascii_letters + "_-", min_size=1, max_size=12),
            children,
            max_size=6,
        )
    ),
    max_leaves=12,
)

tool_args = st.dictionaries(
    st.sampled_from(
        ["path", "cmd", "flags", "query", "url", "timestamp", "session_id", "request_id", "nonce"]
    ),
    json_values,
    min_size=0,
    max_size=6,
)

safe_dirnames = st.sampled_from(["/tmp/tr_prop_root", "/home/tr_prop", "/data/tr_prop"])
safe_basenames = st.text(
    alphabet=string.ascii_letters + string.digits + "_-.", min_size=1, max_size=20
).filter(lambda s: s not in {".", ".."} and "/" not in s)


# ── normalizer ────────────────────────────────────────────────


class TestNormalizerProperties(unittest.TestCase):
    @given(json_values)
    @settings(max_examples=200, deadline=None)
    def test_deterministic_same_input_same_key(self, v):
        """Same input always normalizes to the same string."""
        assert normalize_json(v) == normalize_json(v)

    @given(st.dictionaries(st.text(min_size=1, max_size=16), json_values, max_size=8))
    @settings(max_examples=200, deadline=None)
    def test_key_order_independent(self, d):
        """Insertion order must not affect the normalized key."""
        reordered = dict(reversed(list(d.items())))
        assert normalize_json(d) == normalize_json(reordered)

    @given(
        st.text(alphabet=string.printable, min_size=0, max_size=30),
        st.lists(st.text(alphabet=string.printable, min_size=0, max_size=10), max_size=5),
    )
    @settings(max_examples=200, deadline=None)
    def test_whitespace_and_list_order_independent(self, s, lst):
        """Stripping whitespace and sorting primitive lists must not change
        the key for semantically-equal inputs."""
        a = {"v": f"  {s}  ", "l": lst}
        b = {"v": s.strip(), "l": list(reversed(lst))}
        assert normalize_json(a) == normalize_json(b) or (
            # only differs if list elements are non-primitive (dicts) — but
            # strategy guarantees primitives here, so equality must hold
            False
        ), f"order/whitespace sensitivity: {a!r} vs {b!r}"

    @given(tool_args)
    @settings(max_examples=150, deadline=None)
    def test_noise_keys_never_affect_key(self, args):
        """Adding NON_SEMANTIC keys must never change the cache key."""
        noisy = dict(args)
        noisy["timestamp"] = "whatever"
        noisy["session_id"] = "abc123"
        assert normalize_tool_args(args) == normalize_tool_args(noisy)

    @given(json_values)
    @settings(max_examples=150, deadline=None)
    def test_output_is_valid_json(self, v):
        """Output must be parseable JSON (it feeds json.loads-based hashing)."""
        import json

        json.loads(normalize_json(v))

    @given(st.text(min_size=0, max_size=40))
    @settings(max_examples=150, deadline=None)
    def test_normalize_command_idempotent(self, cmd):
        """normalize_command(normalize_command(c)) == normalize_command(c)."""
        once = normalize_command(cmd)
        assert normalize_command(once) == once

    @given(st.text(alphabet=string.printable, min_size=1, max_size=30))
    @settings(max_examples=150, deadline=None)
    def test_normalize_command_lowercases_only_first_token(self, cmd):
        parts = cmd.strip().split()
        if parts:
            out = normalize_command(cmd)
            expected_first = parts[0].lower()
            assert out.split()[0] == expected_first
            assert out.split()[1:] == parts[1:]


# ── path_utils (security invariants) ─────────────────────────


class TestPathUtilsProperties(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="tr_prop_allow_")
        os.makedirs(os.path.join(self._tmp, "sub"), exist_ok=True)

    def tearDown(self):
        import shutil

        shutil.rmtree(self._tmp, ignore_errors=True)

    @given(safe_dirnames, safe_basenames)
    @settings(max_examples=150, deadline=None)
    def test_prefix_boundary_no_sibling_escape(self, root, name):
        """/root-evil must never match /root (prefix-boundary guard)."""
        try:
            os.makedirs(root, exist_ok=True)
        except OSError:
            self.skipTest("cannot create dir (permissions)")
        inside = os.path.join(root, name)
        sibling = root + "-evil"
        try:
            self.assertTrue(check_path_allowed(inside, [root]))
            self.assertFalse(
                check_path_allowed(sibling, [root]),
                "sibling dir with shared prefix must be denied",
            )
        finally:
            import shutil

            shutil.rmtree(root, ignore_errors=True)
            shutil.rmtree(sibling, ignore_errors=True)

    @given(st.lists(safe_basenames, min_size=1, max_size=4))
    @settings(max_examples=100, deadline=None)
    def test_empty_allowlist_always_denies(self, segments):
        """Fail-closed: no allowed_paths -> everything denied, no matter
        how the path is spelled."""
        p = os.path.join(*segments)
        assert not check_path_allowed(p, [])
        assert not check_path_allowed(p, None)

    @given(st.sampled_from(["", " ", "  ~  ", ".", "..", "-", "/"]))
    @settings(max_examples=20, deadline=None)
    def test_allowlist_of_nonexistent_root_still_rules(self, allowed):
        """Nonexistent allowlist entries: realpath still yields a canonical
        prefix; a path can only be allowed if it normalizes into it."""
        probe = os.path.join("/nonexistent-tr-prop", "child")
        result = check_path_allowed(probe, [allowed])
        # Only allowed if the probe canonicalizes under the allowed dir

        canon_allowed = os.path.realpath(os.path.expanduser(allowed))
        canon_probe = os.path.realpath(os.path.expanduser(probe))
        expected = canon_probe == canon_allowed or canon_probe.startswith(canon_allowed + os.sep)
        assert result == expected


# ── toml_serializer (tomllib round-trip oracle) ───────────────


def json_values_without_containers():
    """Scalars only — used inside inline-table/section round-trips."""
    return (
        st.none()
        | st.booleans()
        | st.integers(min_value=-(2**31), max_value=2**31)
        | st.floats(allow_nan=False, allow_infinity=False, min_value=-1e300, max_value=1e300)
        | st.text(alphabet=string.printable, max_size=30)
    )


class TestTomlSerializerProperties(unittest.TestCase):
    @given(st.text(max_size=60))
    @settings(max_examples=200, deadline=None)
    def test_string_roundtrip_through_tomllib(self, s):
        """Any string survives a dumps -> tomllib.load round-trip."""
        text = toml_serializer.dumps({"k": s})
        parsed = tomllib.loads(text)
        assert parsed == {"k": s}, f"string mangled: {s!r} -> {text!r}"

    @given(st.integers(min_value=-(2**63), max_value=2**63 - 1))
    @settings(max_examples=150, deadline=None)
    def test_int_roundtrip(self, n):
        parsed = tomllib.loads(toml_serializer.dumps({"k": n}))
        assert parsed["k"] == n

    @given(
        st.floats(
            allow_nan=False,
            allow_infinity=False,
            min_value=-1e300,
            max_value=1e300,
        )
    )
    @settings(max_examples=200, deadline=None)
    def test_float_roundtrip(self, f):
        parsed = tomllib.loads(toml_serializer.dumps({"k": f}))
        assert parsed["k"] == f

    @given(st.booleans())
    def test_bool_roundtrip(self, b):
        parsed = tomllib.loads(toml_serializer.dumps({"k": b}))
        assert parsed["k"] is b

    @given(
        st.lists(
            st.one_of(
                st.integers(min_value=-(2**31), max_value=2**31),
                st.text(alphabet=string.ascii_letters + string.digits + " -_", max_size=15),
            ),
            max_size=8,
        )
    )
    @settings(max_examples=150, deadline=None)
    def test_simple_list_roundtrip(self, lst):
        """Lists of primitives round-trip through tomllib."""
        text = toml_serializer.dumps({"k": lst})
        parsed = tomllib.loads(text)
        assert parsed["k"] == lst

    @given(
        st.dictionaries(
            st.text(min_size=1, max_size=12, alphabet=string.ascii_letters + "_"),
            json_values_without_containers(),
            max_size=5,
        )
    )
    @settings(max_examples=120, deadline=None)
    def test_section_roundtrip(self, plain):
        """A flat section [name] with scalar values round-trips.

        None maps to "" (TOML has no null type — documented serializer
        contract, found by hypothesis; bare `key =` lines are invalid TOML).
        """
        text = toml_serializer.dumps({"sec": plain})
        parsed = tomllib.loads(text)
        expected = {k: ("" if v is None else v) for k, v in plain.items()}
        assert parsed["sec"] == expected

    @given(st.dates())
    @settings(max_examples=80, deadline=None)
    def test_date_roundtrip(self, d):
        parsed = tomllib.loads(toml_serializer.dumps({"k": d}))
        assert parsed["k"] == d


if __name__ == "__main__":
    unittest.main()
