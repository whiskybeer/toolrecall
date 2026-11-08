# Copyright (c) 2026 Robin Schultka
# SPDX-License-Identifier: MIT
# Source: https://github.com/whiskybeer/toolrecall
"""RFC 8785 JSON Canonicalization Scheme (JCS) — stdlib implementation.

Produces the canonical JSON serialization used by ``keys.mcp_request_key_v1``
so that independent implementations (any SDK, any language) derive
byte-identical preimages from equal JSON values. This module is pure: no
I/O, no clock, no config reads.

Implements the full normative ruleset:

- Strings (§3.2.2.2): minimal escaping — only ``"``, ``\\`` and the C0
  controls are escaped (short escapes ``\\b \\t \\n \\f \\r`` where they
  exist, ``\\u00XX`` lowercase-hex otherwise); all other code points are
  emitted literally as UTF-8. Strings containing lone surrogates
  (U+D800–U+DFFF) are REJECTED with ``JCSTypeError`` — they are invalid
  Unicode under I-JSON, escaping them ES-style would produce bytes no
  other implementation reproduces, and surrogate KEYS are unsortable
  (their UTF-16 code units do not encode). This differs from
  ``json.dumps`` default (which escapes non-ASCII) and from
  ``json.dumps(ensure_ascii=False)`` (which still emits ``\\u000f``-style
  escapes for control characters, uses ``\\uXXXX`` with the source's
  case, and silently escapes lone surrogates).
- Numbers (§3.2.2.3): ECMAScript ``Number::toString`` over IEEE-754
  doubles. Python's ``repr`` switches to exponent notation outside
  [1e-4, 1e16); ECMAScript outside [1e-6, 1e21); and Python pads the
  exponent to at least two digits (``1e-05``) where ECMAScript does not
  (``1e-5``). Both divergences are corrected here.
- Object keys (§3.2.3): sorted by **UTF-16 code units**, not code points —
  astral characters (surrogate pairs, U+D800..U+DFFF when encoded) sort
  before U+E000..U+FFFF, unlike Python's native code-point ordering.

Per RFC 8785 §3.1 the input must be I-JSON: no duplicate keys (caller's
parsing duty), strings must be valid Unicode, numbers must be IEEE-754
doubles representable, and NaN/Infinity are rejected (they are not JSON
values). Integers are converted through double exactly as an ECMAScript
client would parse them, which is what makes the derivation
cross-implementation: ``10`` (int) and ``10.0`` (float) canonically
serialize identically, and integers beyond 2^53 are rejected rather than
silently rounded (I-JSON interoperability rule).

Tested against the official RFC 8785 appendix vectors
(``tests/data/rfc8785/``) plus property tests (round-trip, key-order
invariance, rejection cases).
"""

from __future__ import annotations

import math
from typing import Any

__all__ = ["canonicalize"]

# ECMAScript Number::toString uses positional (non-exponent) notation for
# decimal exponents in [-6, 21); Python repr uses [-4, 16).
_ES_MIN_EXP = -6
_ES_MAX_EXP = 21


class JCSTypeError(TypeError):
    """Value outside the JSON data model where JCS demands one."""


def _es6_number(value: float) -> str:
    """Format a finite double exactly as ECMAScript Number::toString."""
    if math.isnan(value) or math.isinf(value):
        raise JCSTypeError(f"number {value!r} is not a JSON value (NaN/Infinity)")
    if value == 0:
        return "0"  # covers -0.0 → "0"
    neg = value < 0
    py = repr(abs(value))  # shortest round-trip repr
    if "e" in py or "E" in py:
        # Python scientific form: mantissa 'e' exponent(≥2 digits, signed)
        mantissa, _, exp_part = py.partition("e")
        exp = int(exp_part)
        if _ES_MIN_EXP <= exp < _ES_MAX_EXP:
            # Python went scientific (or would pad the exponent) where
            # ECMAScript stays positional for [-6, 21): rebuild positional
            # form by string surgery on the shortest round-trip digits.
            body = _positional_from_sci(mantissa, exp)
        else:
            # Both engines scientific — fix the exponent's leading zeros
            # (Python pads to 2 digits: '1e-05'; ECMAScript: '1e-5').
            sign = "-" if exp < 0 else "+"
            body = f"{mantissa}e{sign}{abs(exp)}"
    else:
        # Positional in Python; ECMAScript differs only for integral floats
        # (repr '2.0' → '2') and stays positional here (exp < 16 covers it).
        body = py[:-2] if py.endswith(".0") else py
    return ("-" if neg else "") + body


def _positional_from_sci(mantissa: str, exp: int) -> str:
    """Positional decimal from scientific repr parts (string surgery only).

    ``mantissa`` is d(.ddd)? with a nonzero leading digit, ``exp`` its
    decimal exponent: value = 0.<digits> shifted so the first digit sits at
    10**exp. No float arithmetic — exact by construction, and the digit
    string stays the shortest round-trip one (ECMAScript positional form
    keeps every significant digit, e.g. 1.2345e-5 → '0.000012345').
    """
    digits = (mantissa.replace(".", "")).lstrip("0") or "0"
    if exp >= 0:
        if len(digits) > exp + 1:
            return digits[: exp + 1] + "." + digits[exp + 1 :]
        return digits + "0" * (exp + 1 - len(digits))
    return "0." + "0" * (-exp - 1) + digits


def _es6_string(value: str) -> str:
    out = ['"']
    for ch in value:
        code = ord(ch)
        if ch == '"':
            out.append('\\"')
        elif ch == "\\":
            out.append("\\\\")
        elif code == 0x08:
            out.append("\\b")
        elif code == 0x09:
            out.append("\\t")
        elif code == 0x0A:
            out.append("\\n")
        elif code == 0x0C:
            out.append("\\f")
        elif code == 0x0D:
            out.append("\\r")
        elif code < 0x20:
            out.append(f"\\u{code:04x}")
        elif 0xD800 <= code <= 0xDFFF:
            # Lone surrogates are not valid Unicode (I-JSON). Policy: REJECT
            # uniformly — for keys and values, single- and multi-key alike.
            # Escaping them ES-style would emit bytes no other RFC 8785
            # implementation reproduces (libraries disagree on the escape
            # entirely), silently breaking the cross-language byte-equality
            # this module exists for; and a surrogate key is unsortable (its
            # UTF-16 code units do not encode). Callers catch JCSTypeError
            # and surface "no identity asserted".
            raise JCSTypeError(
                f"string contains lone surrogate \\u{code:04X} "
                "(invalid Unicode — not canonicalizable under RFC 8785)"
            )
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def _serialize(value: Any) -> str:
    if value is True:
        return "true"
    if value is False:
        return "false"
    if value is None:
        return "null"
    if isinstance(value, str):
        return _es6_string(value)
    if isinstance(value, int):
        # Route through double exactly as an ECMAScript client parses JSON.
        # Hard boundary: |n| <= 2^53 - 1 = Number.MAX_SAFE_INTEGER — the
        # largest integer every JSON consumer round-trips exactly. 2**53 is
        # itself representable, but integers above it start colliding with
        # neighbors (2^53+1 parses to 2^53), so v1 rejects >= 2^53 outright
        # instead of admitting a window where equality gets fuzzy.
        if abs(value) > 2**53 - 1:
            raise JCSTypeError(
                f"integer {value} exceeds the I-JSON safe range "
                f"(|n| <= 2^53 - 1); reject instead of silently rounding"
            )
        return _es6_number(float(value))
    if isinstance(value, float):
        return _es6_number(value)
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(_serialize(v) for v in value) + "]"
    if isinstance(value, dict):
        # Key validation BEFORE the sort: the UTF-16 sort key cannot encode
        # a lone surrogate (UnicodeEncodeError) and non-str keys would raise
        # TypeError/AttributeError from the comparison itself. Both are
        # caller-hostile leaks; JCSTypeError is the contract.
        for key in value:
            if not isinstance(key, str):
                raise JCSTypeError(f"object keys must be strings, got {type(key).__name__}")
            _reject_lone_surrogates(key, "object key")
        if len(value) > 1:
            # JCS §3.2.3: sort by UTF-16 code units (surrogate-pair order),
            # not Python's code-point order.
            items = sorted(value.items(), key=lambda kv: kv[0].encode("utf-16-be"))
        else:
            items = list(value.items())
        return "{" + ",".join(f"{_es6_string(k)}:{_serialize(v)}" for k, v in items) + "}"
    raise JCSTypeError(f"value of type {type(value).__name__} is outside the JSON data model")


def _reject_lone_surrogates(s: str, what: str) -> None:
    """Raise JCSTypeError when *s* contains a surrogate code point (D800–DFFF).

    Runs before any serialization so the failure mode is uniform
    (JCSTypeError) regardless of position: a lone surrogate that reached
    the UTF-16 sort would instead crash with UnicodeEncodeError, and one
    reaching _es6_string would previously have been escaped ES-style.
    """
    for ch in s:
        if 0xD800 <= ord(ch) <= 0xDFFF:
            raise JCSTypeError(
                f"{what} contains lone surrogate U+{ord(ch):04X} "
                "(invalid Unicode — not canonicalizable under RFC 8785)"
            )


def canonicalize(value: Any) -> str:
    """RFC 8785 canonical JSON of ``value`` as a str.

    Raises ``JCSTypeError`` for values outside the JSON data model:
    NaN/Infinity, over-range integers, non-JSON types, non-string object
    keys, and — uniformly, keys and values — strings containing lone
    surrogates (Python's json.loads happily produces them from escapes
    like ``"\\ud800"``, so hostile tool payloads can carry them). Callers
    surface JCSTypeError as "no identity asserted" rather than
    improvising a preimage; letting UnicodeEncodeError/TypeError escape
    instead was the pre-rejection bug (it also made the policy
    size-dependent: single-key objects passed while multi-key objects
    crashed in the UTF-16 sort).
    """
    if isinstance(value, str):
        _reject_lone_surrogates(value, "string")
    return _serialize(value)
