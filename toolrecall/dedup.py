# Copyright (c) 2026 Robin Schultka
# SPDX-License-Identifier: MIT
# Source: https://github.com/whiskybeer/toolrecall
"""toolrecall.dedup — request-level duplicate-content dedup (transport-neutral core).

Scans a ``messages`` list for large text blocks (and, opt-in, image parts)
that appear more than once in the same request — typically repeated file
reads / tool results in agent loops — and replaces every duplicate AFTER
the first occurrence with a short stub.

Both message-carrying wire shapes are handled structurally, never by vendor
branch: chat-completions (``content`` as a string or a list of ``text`` /
``image_url`` parts) and messages-style payloads, where tool output arrives
inside ``tool_result`` blocks with nested content (Anthropic; Claude Code).
Blocks are shallow-copied and only their text leaf is replaced, so
``cache_control``, ``tool_use_id`` and every other key survive byte-exact.

Design properties
-----------------
* Keep-first, stub-later — within a request, earlier messages are never
  rewritten; the *new* duplicate is what gets stubbed. Across turns the
  byte-prefix stays stable only when the duplicate is stubbed on its FIRST
  send: with ``protect_last > 0`` a duplicate arriving inside the protected
  tail goes out in full, gets cached, and is stubbed one turn later — which
  rewrites already-cached bytes and forces everything after it to be written
  again (~1.25x) instead of read (~0.1x). Use ``protect_last=0`` where the
  provider does prefix caching; keep-first already keeps the full copy.
  Pinned by tests/test_dedup_anthropic.py::TestPrefixStability and
  ::TestProtectedTailVsCache.
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
import re
from typing import Any, Callable, Dict, List, Optional, Tuple

from toolrecall.keys import digest16

# Roles whose content may be replaced with a stub. Assistant/system content is
# registered (so later duplicates elsewhere can reference it) but never modified.
# Messages-style payloads have no "tool" role — tool output rides inside
# tool_result blocks of a "user" message, which is why "user" is stubbable.
DEFAULT_STUB_ROLES = ("tool", "function", "user")

STUB_TEMPLATE = (
    "[toolrecall-dedup] Duplicate content omitted ({chars} chars, sha256:{digest}). "
    "The byte-identical content already appears in message {index} of this request."
)

# Used when a normalizer matched the block: the text is NOT byte-identical to
# the first copy, so the stub must not say so. It keeps this output's own last
# line (usually the runner summary, e.g. "1 failed in 0.91s") so the model gets
# the result without the repeat. Measured on Claude Code: the hash-style stub
# above made the model reason about the omission (output tokens 2x baseline).
NORMALIZED_STUB_TEMPLATE = (
    "[toolrecall-dedup] Same output as the tool result in message {index} "
    "(differs only in timings, timestamps, temp paths or addresses); "
    "{chars} chars not repeated. Last line of this output: {last_line}"
)

IMAGE_STUB_TEMPLATE = (
    "[toolrecall-dedup] Duplicate image omitted ({nbytes} bytes, sha256:{digest}). "
    "The byte-identical image already appears in message {index} of this request."
)

_RE_NORM_DURATION = re.compile(r"\b(in|took) \d+(?:\.\d+)?\s?(?:ms|s|sec|seconds)\b")
_RE_NORM_TEST_SUMMARY_SEC = re.compile(r"(\bpassed(?:,\s*\d+\s*[a-zA-Z]+)* in )\d+(?:\.\d+)?s\b")
_RE_NORM_TIMESTAMP = re.compile(
    r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?\b"
)
_RE_NORM_PYTEST_TEMP = re.compile(
    r"(?:/tmp|/var/tmp|/var/folders/[^/]+/[^/]+)/pytest-of-[^/\s\"']+/pytest-\d+(?:/[^\s\"']*)?"
)
_RE_NORM_PYTEST_NUM = re.compile(r"\bpytest-\d+\b")
_RE_NORM_MEMADDR = re.compile(r"\b0x[0-9a-fA-F]{6,16}\b")
_RE_NORM_PID = re.compile(r"\b(?:PID[:\s=]+|process )\d+\b")


def normalize_output(text: str) -> str:
    """Normalize volatile elements in repetitive tool/bash output.

    Used by deduplication to match recurring command and test outputs where
    minor elapsed durations, timestamps, temporary directory counters, or
    memory addresses vary between turns without changing the diagnostic content.
    """
    if not isinstance(text, str):
        return text
    # 1. Durations
    t = _RE_NORM_DURATION.sub(r"\1 <duration>", text)
    t = _RE_NORM_TEST_SUMMARY_SEC.sub(r"\1<duration>", t)
    # 2. Timestamps
    t = _RE_NORM_TIMESTAMP.sub("<timestamp>", t)
    # 3. Pytest temp directories
    t = _RE_NORM_PYTEST_TEMP.sub("<pytest-tempdir>", t)
    t = _RE_NORM_PYTEST_NUM.sub("<pytest-tempdir>", t)
    # 4. Memory addresses
    t = _RE_NORM_MEMADDR.sub("<memaddr>", t)
    # 5. Process IDs
    t = _RE_NORM_PID.sub("<pid>", t)
    # 6. Normalize CRLF and trailing whitespace per line
    lines = [line.rstrip() for line in t.replace("\r\n", "\n").split("\n")]
    return "\n".join(lines)


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
    normalizer: Optional[Callable[[str], str]] = None,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Return (new_messages, stats). Input objects are never mutated.

    ``media=True`` (experimental, off by default) extends dedup to image
    parts — chat-completions ``image_url`` parts and messages-style
    ``image`` blocks (``{"type": "image", "source": {"type": "base64",
    ...}}``). Scope, deliberately narrow to keep the replay guarantee:

    - Base64 payloads only — chat-completions ``data:<mime>;base64,<payload>``
      URLs and messages-style ``source.data``. Tool-returned screenshots.
      ``https://`` references are NOT deduped: their identity is the URL,
      not the content.
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
    # Per-tool attribution of the savings. Without it the only signal is one
    # number per request, and "is skills_list (or read_file, or …) being
    # stubbed?" cannot be answered. OpenAI-format tool messages carry a
    # tool_call_id that points at the assistant's tool_calls entry, which is
    # where the function name lives.
    call_names: Dict[str, str] = {}
    for msg in messages:
        if isinstance(msg, dict) and msg.get("role") == "assistant":
            for tc in msg.get("tool_calls") or ():
                if isinstance(tc, dict) and isinstance(tc.get("id"), str):
                    fn = tc.get("function") or {}
                    if isinstance(fn, dict) and isinstance(fn.get("name"), str):
                        call_names[tc["id"]] = fn["name"]
    by_tool: Dict[str, Dict[str, int]] = {}
    current_label = "unknown"

    def _attribute(msg: Dict[str, Any]) -> str:
        role = msg.get("role")
        if role in ("tool", "function"):
            name = call_names.get(msg.get("tool_call_id") or "") or msg.get("name")
            return name if isinstance(name, str) and name else "unknown-tool"
        return str(role or "unknown")

    def _count(saved: int) -> None:
        slot = by_tool.setdefault(current_label, {"blocks": 0, "chars_saved": 0})
        slot["blocks"] += 1
        slot["chars_saved"] += saved

    def maybe_stub(text: Any, msg_index: int, can_stub: bool):
        nonlocal blocks, chars_saved
        if not isinstance(text, str) or len(text) < min_chars:
            return None
        key_text = normalizer(text) if normalizer is not None else text
        h = _digest(key_text)
        first = seen.get(h)
        if first is None:
            seen[h] = msg_index  # register first occurrence (any role)
            return None
        if not can_stub:
            return None
        if normalizer is not None:
            last_line = next((ln.strip() for ln in reversed(text.splitlines()) if ln.strip()), "")
            stub = NORMALIZED_STUB_TEMPLATE.format(
                chars=len(text), index=first, last_line=last_line[:200]
            )
        else:
            stub = STUB_TEMPLATE.format(chars=len(text), digest=h, index=first)
        if len(stub) >= len(text):
            return None
        blocks += 1
        chars_saved += len(text) - len(stub)
        _count(len(text) - len(stub))
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
        _count(len(url) - len(stub))
        return stub

    def maybe_stub_image_data(source: dict, msg_index: int, can_stub: bool) -> Optional[str]:
        """Same contract as maybe_stub, for messages-style image blocks.

        ``{"type": "image", "source": {"type": "base64", "media_type": ...,
        "data": <b64>}}`` — the shape a screenshot lands in on Anthropic's
        wire. Identity is the digest of the DECODED bytes, in the same
        keyspace as text and data-URL parts, so the same picture re-sent as
        a tool result stubs down to a short reference. ``chars_saved``
        counts the base64 payload actually sent on the wire.
        """
        nonlocal blocks, chars_saved
        if source.get("type") != "base64" or not isinstance(source.get("data"), str):
            return None
        try:
            decoded = base64.b64decode(source["data"], validate=False)
        except (binascii.Error, ValueError):
            return None
        if not decoded or len(decoded) < min_chars:
            return None
        h = digest16(decoded)
        first = seen.get(h)
        if first is None:
            seen[h] = msg_index
            return None
        if not can_stub:
            return None
        stub = IMAGE_STUB_TEMPLATE.format(nbytes=len(decoded), digest=h, index=first)
        if len(stub) >= len(source["data"]):
            return None
        blocks += 1
        chars_saved += len(source["data"]) - len(stub)
        return stub

    def walk_content(content: Any, msg_index: int, can_stub: bool):
        """Dedup a content value; returns ``(value, changed)``.

        Handles both message-carrying wire shapes STRUCTURALLY (no vendor
        branch, no framework import):

        - chat-completions: ``content`` is a string, or a list of parts
          (``text``, ``image_url``);
        - messages-style (Anthropic): ``content`` is a list of blocks
          (``text``, ``tool_result`` with nested content, ``image``).

        ``tool_result`` is where an agent loop's repeated payload actually
        lives — one level below the block — so the walk recurses into it.
        Blocks are shallow-copied and only the text leaf is replaced:
        ``cache_control``, ``tool_use_id``, ``name`` and every other key
        survive byte-exact, which is what an exact-prefix provider cache
        (Claude Code) needs to keep hitting.
        """
        if isinstance(content, str):
            stub = maybe_stub(content, msg_index, can_stub)
            return (stub, True) if stub is not None else (content, False)
        if not isinstance(content, list):
            return content, False
        new_parts = None
        for j, part in enumerate(content):
            if not isinstance(part, dict):
                continue
            ptype = part.get("type")
            if ptype == "text" and isinstance(part.get("text"), str):
                stub = maybe_stub(part["text"], msg_index, can_stub)
                if stub is not None:
                    if new_parts is None:
                        new_parts = list(content)
                    p2 = copy.copy(part)
                    p2["text"] = stub
                    new_parts[j] = p2
            elif ptype == "tool_result" and isinstance(part.get("content"), (str, list)):
                inner, inner_changed = walk_content(part["content"], msg_index, can_stub)
                if inner_changed:
                    if new_parts is None:
                        new_parts = list(content)
                    p2 = copy.copy(part)
                    p2["content"] = inner
                    new_parts[j] = p2
            elif (
                media
                and ptype == "image_url"
                and isinstance(part.get("image_url"), dict)
                and isinstance(part["image_url"].get("url"), str)
            ):
                stub = maybe_stub_media(part["image_url"]["url"], msg_index, can_stub)
                if stub is not None:
                    if new_parts is None:
                        new_parts = list(content)
                    # Replace the ENTIRE image part with a text stub part.
                    # Providers validate image_url.url as a URL (OpenRouter
                    # 400 "Invalid URL format" — caught in the gate probe),
                    # so the stub must live in a text part, which every
                    # provider accepts and the model can read.
                    new_parts[j] = {"type": "text", "text": stub}
            elif media and ptype == "image" and isinstance(part.get("source"), dict):
                stub = maybe_stub_image_data(part["source"], msg_index, can_stub)
                if stub is not None:
                    if new_parts is None:
                        new_parts = list(content)
                    # Same rule as image_url: a text block is valid on every
                    # surface and readable by the model; the base64 is not.
                    new_parts[j] = {"type": "text", "text": stub}
        if new_parts is None:
            return content, False
        return new_parts, True

    for i, msg in enumerate(messages):
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        content = msg.get("content")
        can_stub = role in stub_roles and role != "system" and i < protect_from
        current_label = _attribute(msg)

        if isinstance(content, (str, list)):
            new_content, changed = walk_content(content, i, can_stub)
            if changed:
                m2 = copy.copy(msg)
                m2["content"] = new_content
                out[i] = m2

    return out, {
        "blocks": blocks,
        "chars_saved": chars_saved,
        # chars/4 heuristic, logging only — TEXT-calibrated. For image stubs
        # it OVERSTATES provider savings (image tokens are priced per image,
        # not per char); real multimodal figures come from the bench harness.
        "est_tokens_saved": chars_saved // 4,
        # {tool_name|role: {"blocks", "chars_saved"}} — which tool produced the
        # duplicate that was stubbed. Sums equal blocks / chars_saved above.
        "by_tool": by_tool,
    }
