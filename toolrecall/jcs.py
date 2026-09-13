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
  exist, ``\\u00XX`` lowercase-hex otherwise); lone surrogates are escaped
  ``\\udXXX`` as ECMAScript well-formed JSON.stringify does (keeps output
  strictly UTF-8-encodable and lossless); all other code points are
  emitted literally as UTF-8. This differs from ``json.dumps`` default
  (which escapes non-ASCII) and from ``json.dumps(ensure_ascii=False)``
  (which still emits ``\\u000f``-style escapes for control characters and
  uses ``\\uXXXX`` with the source's case).
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
            # Lone surrogates are not valid Unicode (I-JSON); escape them as
            # ECMAScript well-formed JSON.stringify does (ES2019+) so the
            # output stays strictly UTF-8 encodable and lossless.
            out.append(f"\\u{code:04x}")
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
        if abs(value) > 2**53:
            raise JCSTypeError(
                f"integer {value} exceeds IEEE-754 exact range (I-JSON); "
                "reject instead of silently rounding"
            )
        return _es6_number(float(value))
    if isinstance(value, float):
        return _es6_number(value)
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(_serialize(v) for v in value) + "]"
    if isinstance(value, dict):
        if len(value) > 1:
            # JCS §3.2.3: sort by UTF-16 code units (surrogate-pair order),
            # not Python's code-point order.
            items = sorted(value.items(), key=lambda kv: kv[0].encode("utf-16-be"))
        else:
            items = list(value.items())
        return "{" + ",".join(f"{_es6_string(k)}:{_serialize(v)}" for k, v in items) + "}"
    raise JCSTypeError(f"value of type {type(value).__name__} is outside the JSON data model")


def canonicalize(value: Any) -> str:
    """RFC 8785 canonical JSON of ``value`` as a str (UTF-8 encodable).

    Raises ``JCSTypeError`` for values outside the JSON data model
    (NaN/Infinity, over-range integers, non-JSON types) — callers surface
    that as "no identity asserted" rather than improvising a preimage.
    """
    return _serialize(value)
