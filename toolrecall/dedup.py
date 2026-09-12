# Copyright (c) 2026 Robin Schultka
# SPDX-License-Identifier: MIT
# Source: https://github.com/whiskybeer/toolrecall
"""toolrecall.dedup — request-level duplicate-content dedup (transport-neutral core).

Scans a chat-completion ``messages`` list for large text blocks (and, opt-in,
data-URL image parts) that appear more than once in the same request —
typically repeated file reads / tool results in agent loops — and replaces
every duplicate AFTER the first occurrence with a short stub.

Design properties
-----------------
* Keep-first, stub-later — earlier messages are never rewritten when a new
  duplicate appears later, so the byte-prefix of the request stays stable
  across turns. Provider-side prompt caching (Anthropic/OpenAI/DeepSeek)
  keeps hitting.
* Deterministic — same messages + same config → same output, every time.
* Pure stdlib, no I/O, no globals — safe to call from any transport layer
  (LiteLLM hook, forward proxy, batch tools).

This is the single implementation the ``toolrecall.adapters.litellm`` hook
re-exports; the forward proxy calls it directly. One algorithm, one test
surface — transport layers only decide WHERE it fires and what env/config
controls it.
"""

from __future__ import annotations

import base64
import binascii
import copy
from typing import Any, Dict, List, Optional, Tuple

from toolrecall.keys import digest16

# Roles whose content may be replaced with a stub. Assistant/system content is
# registered (so later duplicates elsewhere can reference it) but never modified.
DEFAULT_STUB_ROLES = ("tool", "function", "user")

STUB_TEMPLATE = (
    "[toolrecall-dedup] Duplicate content omitted ({chars} chars, sha256:{digest}). "
    "The byte-identical content already appears in message {index} of this request."
)

IMAGE_STUB_TEMPLATE = (
    "[toolrecall-dedup] Duplicate image omitted ({nbytes} bytes, sha256:{digest}). "
    "The byte-identical image already appears in message {index} of this request."
)


def _decode_data_url(url: Any) -> Optional[bytes]:
    """Decode a ``data:<mime>;base64,<payload>`` URL to raw bytes, else None.

    ``validate=False`` (b64decode default) is deliberate: alternate base64
    wrapping/whitespace still decodes, so identity is the decoded bytes, not
    the wrap. Non-data URLs (``https://``) return None — their identity is
    the URL, not content this module can see. Decoded bytes are only ever
    hashed, never emitted.
    """
    if not isinstance(url, str) or not url.startswith("data:") or ";base64," not in url:
        return None
    payload = url.split(";base64,", 1)[1]
    try:
        return base64.b64decode(payload)
    except (binascii.Error, ValueError):
        return None


def _digest(text: str) -> str:
    """16-hex content digest — delegates to the canonical Layer-0 primitive.

    Legacy width (16 hex) is preserved for stub references already emitted
    in live sessions; the canonical form is ``toolrecall.keys.content_digest``.
    """
    return digest16(text)


def dedup_messages(
    messages: List[Dict[str, Any]],
    *,
    min_chars: int = 800,
    protect_last: int = 2,
    stub_roles: Tuple[str, ...] = DEFAULT_STUB_ROLES,
    media: bool = False,
) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    """Return (new_messages, stats). Input objects are never mutated.

    ``media=True`` (experimental, off by default) extends dedup to image
    parts (``{"type": "image_url", "image_url": {"url": ...}}``). Scope,
    deliberately narrow to keep the replay guarantee:

    - data URLs only (``data:<mime>;base64,<payload>``) — tool-returned
      screenshots. ``https://`` references are NOT deduped: their identity
      is the URL, not the content.
    - Hashed over the DECODED bytes, so the same image with different
      base64 wrapping/whitespace still hits.
    - One identity space with text: a digest means "these bytes appeared
      earlier in this request", regardless of which part type carried them.
    - Byte floor is ``min_chars`` applied to decoded byte length; a stub
      must also be strictly cheaper than the part it replaces.
    - A duplicate image part is REPLACED BY A SHORT TEXT PART (not a stub
      inside ``image_url.url``): providers validate the URL field (OpenRouter
      rejects stub text with 400 "Invalid URL format"), while a text part is
      universally valid and readable by the model. The mime label is NOT
      part of the identity: identical bytes under different labels dedup.

    The ``protect_last`` tail is the stub armor: consecutive image stubs
    can never reach the final N messages, so a harness that compacts old
    content always has the latest full copies.
    """
    n = len(messages)
    protect_from = max(0, n - max(0, protect_last))
    seen: Dict[str, int] = {}  # digest -> index of first occurrence
    out = list(messages)
    blocks = 0
    chars_saved = 0

    def maybe_stub(text: Any, msg_index: int, can_stub: bool):
        nonlocal blocks, chars_saved
        if not isinstance(text, str) or len(text) < min_chars:
            return None
        h = _digest(text)
        first = seen.get(h)
        if first is None:
            seen[h] = msg_index  # register first occurrence (any role)
            return None
        if not can_stub:
            return None
        stub = STUB_TEMPLATE.format(chars=len(text), digest=h, index=first)
        if len(stub) >= len(text):
            return None
        blocks += 1
        chars_saved += len(text) - len(stub)
        return stub

    def maybe_stub_media(url: str, msg_index: int, can_stub: bool) -> Optional[str]:
        """Same contract as maybe_stub, for data-URL image parts.

        Identity = digest of the DECODED bytes (shared keyspace with text:
        digest over the part's byte payload, whatever the part type). Byte
        floor is min_chars on decoded length — image tokens are priced per
        image, so min_chars under-selects unless callers lower it.
        """
        nonlocal blocks, chars_saved
        decoded = _decode_data_url(url)
        if decoded is None or len(decoded) < min_chars:
            return None
        h = digest16(decoded)
        first = seen.get(h)
        if first is None:
            seen[h] = msg_index
            return None
        if not can_stub:
            return None
        stub = IMAGE_STUB_TEMPLATE.format(nbytes=len(decoded), digest=h, index=first)
        if len(stub) >= len(url):
            return None
        blocks += 1
        chars_saved += len(url) - len(stub)
        return stub

    for i, msg in enumerate(messages):
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        content = msg.get("content")
        can_stub = role in stub_roles and role != "system" and i < protect_from

        if isinstance(content, str):
            stub = maybe_stub(content, i, can_stub)
            if stub is not None:
                m2 = copy.copy(msg)
                m2["content"] = stub
                out[i] = m2
        elif isinstance(content, list):  # OpenAI content-parts format
            new_parts = None
            for j, part in enumerate(content):
                if (
                    isinstance(part, dict)
                    and part.get("type") == "text"
                    and isinstance(part.get("text"), str)
                ):
                    stub = maybe_stub(part["text"], i, can_stub)
                    if stub is not None:
                        if new_parts is None:
                            new_parts = list(content)
                        p2 = copy.copy(part)
                        p2["text"] = stub
                        new_parts[j] = p2
                elif (
                    media
                    and isinstance(part, dict)
                    and part.get("type") == "image_url"
                    and isinstance(part.get("image_url"), dict)
                    and isinstance(part["image_url"].get("url"), str)
                ):
                    url = part["image_url"]["url"]
                    stub = maybe_stub_media(url, i, can_stub)
                    if stub is not None:
                        if new_parts is None:
                            new_parts = list(content)
                        # Replace the ENTIRE image part with a text stub part.
                        # Providers validate image_url.url as a URL (OpenRouter
                        # 400 "Invalid URL format" — caught in the gate probe),
                        # so the stub must live in a text part, which every
                        # provider accepts and the model can read.
                        new_parts[j] = {"type": "text", "text": stub}
            if new_parts is not None:
                m2 = copy.copy(msg)
                m2["content"] = new_parts
                out[i] = m2

    return out, {
        "blocks": blocks,
        "chars_saved": chars_saved,
        # chars/4 heuristic, logging only — TEXT-calibrated. For image stubs
        # it OVERSTATES provider savings (image tokens are priced per image,
        # not per char); real multimodal figures come from the bench harness.
        "est_tokens_saved": chars_saved // 4,
    }
