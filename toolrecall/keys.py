# Copyright (c) 2026 Robin Schultka
# SPDX-License-Identifier: MIT
# Source: https://github.com/whiskybeer/toolrecall
"""Canonical key derivation for ToolRecall — the Layer-0 primitive.

One module for turning tool requests and tool results into stable,
content-addressed keys. Everything that hashes tool input or tool output
routes through here, so a given (tool, args) pair or a given result body
hashes identically in every adapter, in the daemon, and in external code
that imports this module standalone.

Two primitives, one rule — hash the canonical thing, nothing else:

- ``content_digest(data)``  — content key. sha256 over the exact bytes of a
  tool RESULT (file body, command output, screenshot bytes). Freshness, TTL,
  and staleness are policy decided by callers; this function only answers
  "what are the bytes' identity?"

- ``tool_key(tool, args, kwargs)`` — request key. sha256 over a canonical
  JSON preimage of a tool REQUEST. Deterministic under dict insertion-order
  permutation.

Design constraints (deliberate):

- Pure functions. No I/O, no config reads, no clock, no logging inside.
  Same input bytes → same key, on any machine, forever. This is what makes
  record/replay and cross-session cache hits possible at all.
- stdlib only (hashlib, json) — zero-runtime-deps is a hard identity.
- Backwards compatibility is part of the contract. The Stage-0 consolidation
  (2026-09) keeps every preimage byte-identical to the pre-consolidation
  adapters; vectors in ``tests/golden_keys.json`` are pinned by tests.

Known policy deltas, documented honestly:

- Encoding policy: ``content_digest`` encodes str with
  ``errors="surrogatepass"``. Pre-Stage-0, ``mcp_bridge`` used
  ``errors="replace"`` and ``litellm._digest`` used ``surrogatepass``. For
  all valid text the two are byte-identical (proved by golden vectors);
  the ONLY divergence is text containing lone surrogates, which is invalid
  UTF-8 either way. ``surrogatepass`` wins because it is lossless (no
  U+FFFD collision) and round-trips.
- Truncation policy: ``litellm._digest`` returned 16-hex; the bridge
  returned 64-hex. Truncation is now an explicit ``truncate=`` argument at
  call sites — the digest itself is always full-width. Persisted 16-hex
  keys keep their exact legacy values.
- Normalization limitation: ``tool_key`` coerces non-JSON-safe values with
  ``str()`` (legacy behavior, kept for key compat). Nested dicts inside
  such values therefore keep their runtime repr ordering: deterministic per
  process and per equal-structure object, but NOT canonical across
  differently-ordered equivalent dicts. The canonical-nesting fix changes
  key bytes and ships in a later stage behind a key-version bump, not here.

Phase 2 (2026-09) — routed ad-hoc sha256 sites through here: proxy
api-cache/stream/log hashes (now via ``api_request_hash`` /
``content_digest``), daemon ping config_hash, mcp_bridge config-hash
filename, replay._make_hash. Refused, with reasons: ``_db._hash`` and the
daemon PID lock (md5 domains with persisted keys), ``cache.py`` MCP
preimages (legacy ``server://tool?args`` format ≠ tool_key), and
``adapters/warp.make_request_hash`` (stdlib-only edge that deploys without
a toolrecall install — parity enforced by tests instead of import).
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Optional, Tuple, Union

__all__ = ["content_digest", "tool_key", "digest16", "api_request_hash"]

BytesLike = Union[str, bytes, bytearray]
_MIN_TRUNCATE = 8  # below 8 hex chars, stub references collide too easily
_MAX_TRUNCATE = 64  # sha256 hexdigest width


def content_digest(data: BytesLike, *, truncate: Optional[int] = None) -> str:
    """Full sha256 hexdigest of tool-result content — the content key.

    Accepts str (encoded UTF-8 with ``surrogatepass``, lossless for any
    Python str) or bytes/bytearray (hashed as-is). This is the single
    encoding policy for every adapter, daemon table, and stub reference;
    do not hand-roll ``text.encode(...)`` elsewhere.

    ``truncate=N`` returns the first N hex chars for bounded-width callers
    (e.g. 16 for legacy stub references). The full digest is always the
    canonical form; truncation is a caller-side presentation choice.
    """
    if isinstance(data, str):
        payload = data.encode("utf-8", "surrogatepass")
    elif isinstance(data, (bytes, bytearray)):
        payload = bytes(data)
    else:
        raise TypeError(f"content_digest expects str|bytes|bytearray, got {type(data).__name__}")
    digest = hashlib.sha256(payload).hexdigest()
    if truncate is None:
        return digest
    if not isinstance(truncate, int) or not (_MIN_TRUNCATE <= truncate <= _MAX_TRUNCATE):
        raise ValueError(f"truncate must be an int in [{_MIN_TRUNCATE}, {_MAX_TRUNCATE}]")
    return digest[:truncate]


def digest16(data: BytesLike) -> str:
    """Bounded 16-hex content digest — legacy stub-reference width."""
    return content_digest(data, truncate=16)


def api_request_hash(method: str, host: str, path: str, body: bytes) -> str:
    """Full sha256 of a proxied API request — the api_cache request key.

    Single source of truth for the proxy↔edge request-hash derivation
    (``f"{method}:{host}:{path}:{body_hash}"`` over the body's sha256).
    Extracted from twin copies in ``proxy.py`` and ``adapters/warp.py``;
    the twins' drift already caused a live 0%-hit-rate incident
    (2026-09-03). Warp-side tooling calls this to predict cache hits
    without a network call. Preimage and width are byte-frozen by
    ``tests/test_keys.py`` and ``tests/test_warp_adapter.py``.
    """
    body_hash = content_digest(body)
    return content_digest(f"{method}:{host}:{path}:{body_hash}")


def _normalize_args(args: Tuple[Any, ...], kwargs: dict) -> str:
    """Canonical JSON preimage for a tool request.

    Byte-identical to the pre-Stage-0 ``odysseus._build_cache_key``
    preimage (including json.dumps' default ``", "/": "`` separators):
    positional args under ``_pos`` (str-coerced), kwargs JSON-safe with
    ``str()`` fallback, object keys sorted, non-ASCII preserved. Pinned by
    ``tests/golden_keys.json``.
    """
    arg_dict: dict = {}
    if args:
        arg_dict["_pos"] = [str(a) for a in args]
    for k, v in kwargs.items():
        # Normalize to JSON-safe types (legacy coercion — see module notes
        # on the nested-ordering limitation this carries).
        if isinstance(v, (str, int, float, bool, type(None))):
            arg_dict[k] = v
        else:
            arg_dict[k] = str(v)
    return json.dumps(arg_dict, sort_keys=True, ensure_ascii=False)


def tool_key(
    tool: str,
    args: Tuple[Any, ...] = (),
    kwargs: Optional[dict] = None,
    *,
    digest: bool = True,
) -> str:
    """Deterministic request key for a tool call.

    ``digest=True`` (default): sha256 hexdigest binding the tool name and
    the canonical argument preimage — the request key for cache tables.
    ``digest=False``: the canonical preimage itself (legacy cache-table
    format, where the tool name is a separate namespace column; also handy
    for debugging — it is human-readable). The preimage intentionally does
    NOT embed the tool name (legacy byte-compat); the digest path does.

    Deterministic under dict insertion-order permutation of ``kwargs``.
    Same (tool, args, kwargs) in any process, any session → same key.
    """
    preimage = _normalize_args(tuple(args), dict(kwargs or {}))
    if not digest:
        return preimage
    bound = json.dumps(
        {"args": preimage, "tool": tool},
        sort_keys=True,
        ensure_ascii=False,
    )
    return content_digest(bound)


# ---------------------------------------------------------------------------
# SEP draft: MCP Tool Result Identity — version-tagged v1 derivations
# ---------------------------------------------------------------------------
# These implement the consumer-derivable identity contract proposed in
# the "SEP: Tool Result Identity" draft (SEP PR + origin discussion URL
# to be inserted here once posted; prior art: MCP discussion #2964 —
# locked, modelcontextprotocol/modelcontextprotocol#2964):
#
#   preimage        = utf8("mcp-request-key-v1") ∥ 0x00 ∥ utf8(JCS{tool, arguments})
#   requestKey      = "sha256:" ∥ hex(sha256(preimage))
#   digest preimage = utf8("mcp-content-digest-v1") ∥ 0x00 ∥ utf8(JCS(content))
#   contentDigest   = "sha256:" ∥ hex(sha256(digest preimage))
#
# Deltas vs. legacy tool_key (all deliberate, all visible to reviewers):
# - Canonicalization is RFC 8785 (JCS, toolrecall/jcs.py), not json.dumps:
#   cross-implementation byte-equality is the whole point of the v1 format.
# - The tool name lives INSIDE the canonical preimage (not a separate
#   namespace column) — the collision-safety rule the SEP makes normative.
# - int/float are deliberately indistinguishable (10 == 10.0): JCS routes
#   numbers through IEEE-754 doubles exactly as an ECMAScript client parses
#   JSON. Legacy tool_key keeps its int/float distinction (byte-compat).
# - Values outside the JSON data model raise JCSTypeError — the caller
#   surfaces "no identity asserted" rather than improvising a preimage.
# - A full-width digest with an "sha256:" algorithm prefix; no truncation
#   parameter (short references are a consumer-side presentation choice,
#   per the SEP never global identifiers).
#
# Version tag: any future change to these preimages MUST bump the tag
# ("mcp-request-key-v2" / "mcp-content-digest-v2" ...), making old/new
# keys coexist unambiguously. Both derivations carry the tag INSIDE the
# hashed preimage, so the two outputs are never confusable — and two
# variants of the same derivation (e.g. JCS-of-content vs exact wire
# bytes, the draft's open question) would produce visibly different
# digests instead of colliding as identical "sha256:<64hex>" strings.

_MCP_REQUEST_KEY_V1_TAG = b"mcp-request-key-v1"
_MCP_CONTENT_DIGEST_V1_TAG = b"mcp-content-digest-v1"


def mcp_request_key_v1(tool: str, arguments: Any) -> str:
    """Canonical request key per the MCP Tool Result Identity v1 draft.

    Consumer-derivable: computable from (tool, arguments) alone, before or
    after the call, on any machine, in any language with an RFC 8785
    serializer. Raises JCSTypeError for values outside the JSON data model.

    ``arguments=None`` (or an omitted arguments field) normalizes to
    ``{}`` (F18): the transport-level absence of arguments and an empty
    argument object are the same call for identity purposes.
    """
    from toolrecall.jcs import canonicalize

    if arguments is None:
        arguments = {}
    canonical = canonicalize({"tool": tool, "arguments": arguments})
    payload = _MCP_REQUEST_KEY_V1_TAG + b"\x00" + canonical.encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def mcp_content_digest_v1(
    content: Any,
    structuredContent: Any = None,
    isError: bool = False,
) -> str:
    """Canonical content digest of a tool result (v1 draft, fix F20).

    Full-width sha256 over the version-tagged JCS preimage of the THREE
    result fields that define what a client would act on::

        preimage = utf8("mcp-content-digest-v1") ∥ 0x00
                 ∥ utf8(JCS({"content": content,
                             "structuredContent": structuredContent,
                             "isError": isError}))
        contentDigest = "sha256:" ∥ hex(sha256(preimage))

    Hashing all three (not just ``content``) closes the silent-failure
    hole where two results differ ONLY in structuredContent or error
    status yet would share a digest — exactly the case a cache MUST
    distinguish. The tag inside the preimage keeps contentDigest
    structurally disjoint from requestKey and from any future derivation
    variant (e.g. hashing exact wire bytes instead of JCS — the draft's
    open question), which would otherwise collide as identical
    "sha256:<64hex>" strings. JCS is applied to the *logical* values; if
    the working group instead pins *exact wire bytes*, the derivation
    changes only inside this function — the tag makes variants visible
    in the output bytes, and a v2 tag bump remains the rule.

    Raises JCSTypeError for values outside the JSON data model
    (including lone surrogates — invalid Unicode, rejected uniformly).
    ``structuredContent`` defaults to None (JSON null in the preimage),
    so a pure-text result digests identically whether or not the server
    set the field. ``isError`` MUST be a bool — truthy coercion of a
    string ``"false"`` into True would be a silent correctness bug
    (review round 2); any non-bool raises TypeError.
    """
    from toolrecall.jcs import canonicalize

    if not isinstance(isError, bool):
        raise TypeError(
            f"isError must be a bool, got {type(isError).__name__} — "
            'truthy coercion would flip string "false" to True'
        )
    preimage = {
        "content": content,
        "structuredContent": structuredContent,
        "isError": isError,
    }
    canonical = canonicalize(preimage)
    payload = _MCP_CONTENT_DIGEST_V1_TAG + b"\x00" + canonical.encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()
