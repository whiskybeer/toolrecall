# Copyright (c) 2026 Robin Schultka
# SPDX-License-Identifier: MIT
# Source: https://github.com/whiskybeer/toolrecall
"""
toolrecall.adapters.litellm — request-level duplicate-content dedup for LiteLLM Proxy.

Plugs into LiteLLM's ``async_pre_call_hook`` (fires just before the request is sent
to the provider). Scans ``data["messages"]`` for large text blocks that appear more
than once in the same request — typically repeated file reads / tool results in agent
loops — and replaces every duplicate AFTER the first occurrence with a short stub.

Design properties
-----------------
* Keep-first, stub-later — earlier messages are never rewritten when a new
  duplicate appears later, so the byte-prefix of the request stays stable across
  turns. Provider-side prompt caching (Anthropic/OpenAI/DeepSeek) keeps hitting.
* Deterministic — same messages + same config → same output, every time.
* Fails open — any exception → request passes through unmodified.
* Zero dependencies beyond litellm itself. Pure stdlib.

What it does NOT do
-------------------
This is standalone request-level dedup, not ToolRecall's drop-and-recall — a
gateway never sees the agent's tool loop, so there is nobody to serve a recall.
It composes with (and does NOT require) the ToolRecall daemon.

Install
-------
In your proxy_config.yaml::

    litellm_settings:
      callbacks: toolrecall.adapters.litellm.handler

Optional env config::

    TOOLRECALL_DEDUP_MIN_CHARS=800      # only dedup blocks >= this size
    TOOLRECALL_DEDUP_PROTECT_LAST=2     # never stub inside the last N messages
    TOOLRECALL_DEDUP_DISABLED=1         # kill switch

Known limitation
----------------
LiteLLM currently bypasses proxy-level ``async_pre_call_hook`` on the
Anthropic-format endpoint ``/v1/messages`` (BerriAI/litellm#27518). Route
requests through the OpenAI-format ``/v1/chat/completions`` endpoint for the
hook to fire.

Self-test:  python3 -m toolrecall.adapters.litellm
"""

from __future__ import annotations

import copy
import hashlib
import logging
import os
from typing import Any, Dict, List, Tuple

try:  # pragma: no cover — import guard so the module also loads outside litellm
    from litellm.integrations.custom_logger import CustomLogger

    _HAVE_LITELLM = True
except Exception:  # noqa: BLE001

    class CustomLogger:  # type: ignore[no-redef]
        """Fallback shim so the dedup logic is importable/testable without litellm."""

    _HAVE_LITELLM = False

log = logging.getLogger("toolrecall.dedup")

# Roles whose content may be replaced with a stub. Assistant/system content is
# registered (so later duplicates elsewhere can reference it) but never modified.
DEFAULT_STUB_ROLES = ("tool", "function", "user")

STUB_TEMPLATE = (
    "[toolrecall-dedup] Duplicate content omitted ({chars} chars, sha256:{digest}). "
    "The byte-identical content already appears in message {index} of this request."
)


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()[:16]


def dedup_messages(
    messages: List[Dict[str, Any]],
    *,
    min_chars: int = 800,
    protect_last: int = 2,
    stub_roles: Tuple[str, ...] = DEFAULT_STUB_ROLES,
) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    """Return (new_messages, stats). Input objects are never mutated."""
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
            if new_parts is not None:
                m2 = copy.copy(msg)
                m2["content"] = new_parts
                out[i] = m2

    return out, {
        "blocks": blocks,
        "chars_saved": chars_saved,
        "est_tokens_saved": chars_saved // 4,  # chars/4 heuristic, logging only
    }


class ToolRecallDedupHandler(CustomLogger):
    """LiteLLM CustomLogger that dedups repeated large text blocks per request."""

    def __init__(self) -> None:
        self.min_chars = int(os.getenv("TOOLRECALL_DEDUP_MIN_CHARS", "800"))
        self.protect_last = int(os.getenv("TOOLRECALL_DEDUP_PROTECT_LAST", "2"))
        self.enabled = os.getenv("TOOLRECALL_DEDUP_DISABLED", "").lower() not in (
            "1",
            "true",
            "yes",
        )
        # process-lifetime counters, exposed for debugging
        self.total_requests_modified = 0
        self.total_blocks = 0
        self.total_chars_saved = 0

    # LiteLLM proxy hook — signature per docs.litellm.ai/docs/proxy/call_hooks
    async def async_pre_call_hook(  # type: ignore[override]
        self,
        user_api_key_dict: Any,
        cache: Any,
        data: dict,
        call_type: str,
    ):
        try:
            if not self.enabled or call_type not in ("completion", "acompletion"):
                return data
            messages = data.get("messages")
            if not isinstance(messages, list) or len(messages) < 3:
                return data

            new_messages, stats = dedup_messages(
                messages,
                min_chars=self.min_chars,
                protect_last=self.protect_last,
            )
            if stats["blocks"]:
                data["messages"] = new_messages
                self.total_requests_modified += 1
                self.total_blocks += stats["blocks"]
                self.total_chars_saved += stats["chars_saved"]
                log.info(
                    "toolrecall-dedup: stubbed %d duplicate block(s), "
                    "saved ~%d chars (~%d tokens est.)",
                    stats["blocks"],
                    stats["chars_saved"],
                    stats["est_tokens_saved"],
                )
        except Exception:  # noqa: BLE001 — a gateway plugin must fail open
            log.exception("toolrecall-dedup failed open; request passed through")
        return data


# Instance referenced from proxy_config.yaml:
#   litellm_settings:
#     callbacks: toolrecall.adapters.litellm.handler
handler = ToolRecallDedupHandler()

# ---------------------------------------------------------------------------
# Self-test:  python3 -m toolrecall.adapters.litellm
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import asyncio

    FILE_A = "def cache_lookup(key):\n    ...\n" * 60  # ~1.9K chars
    FILE_B = "SELECT * FROM turn_log;\n" * 80  # ~1.9K chars

    # NOTE: matching is whole-block exact (hash of the full string / text part),
    # not substring search. A file embedded inside a longer string is a
    # different block and will not match.
    def convo():
        return [
            {"role": "system", "content": "You are a code reviewer."},
            {"role": "user", "content": "Review cache.py"},
            {"role": "assistant", "content": None, "tool_calls": [{"id": "1"}]},
            {"role": "tool", "content": FILE_A},  # first A -> kept
            {"role": "assistant", "content": "Looks fine. Reading schema."},
            {"role": "tool", "content": [{"type": "text", "text": FILE_B}]},  # first B -> kept
            {"role": "user", "content": "Re-read both files."},
            {"role": "tool", "content": FILE_A},  # dup -> stub
            {"role": "tool", "content": [{"type": "text", "text": FILE_B}]},  # dup -> stub
            {"role": "assistant", "content": "Re-read done."},
            {"role": "tool", "content": FILE_A},  # dup but inside protect_last=2 -> kept
            {"role": "user", "content": "Summarize."},
        ]

    msgs = convo()
    out, stats = dedup_messages(msgs, min_chars=800, protect_last=2)

    assert stats["blocks"] == 2, stats
    assert out[3]["content"] == FILE_A  # first occurrence kept in full
    assert out[5]["content"][0]["text"] == FILE_B  # first B kept
    assert out[7]["content"].startswith("[toolrecall-dedup]")
    assert "message 3" in out[7]["content"]  # points at first occurrence
    assert out[8]["content"][0]["text"].startswith("[toolrecall-dedup]")
    assert "message 5" in out[8]["content"][0]["text"]
    assert out[10]["content"] == FILE_A  # protected tail kept
    assert msgs[7]["content"] == FILE_A  # input not mutated

    # Determinism
    out2, stats2 = dedup_messages(convo(), min_chars=800, protect_last=2)
    assert out == out2 and stats == stats2

    # Prefix stability: appending new messages must not change earlier bytes
    longer = convo() + [
        {"role": "assistant", "content": "One more look."},
        {"role": "tool", "content": FILE_B},
        {"role": "user", "content": "Done?"},
    ]
    out3, _ = dedup_messages(longer, min_chars=800, protect_last=2)
    assert out3[:10] == out[:10]
    # message 10 (FILE_A dup) was protected before, is stubbable now -> allowed to change
    assert out3[10]["content"].startswith("[toolrecall-dedup]")
    assert out3[13]["content"] == FILE_B  # new B dup inside protect_last=2 -> kept

    # Hook end-to-end (fails open, modifies in place)
    async def run_hook():
        h = ToolRecallDedupHandler()
        data = {"messages": convo(), "model": "gpt-x"}
        res = await h.async_pre_call_hook(None, None, data, "acompletion")
        assert res is data and h.total_blocks == 2
        # garbage input -> passes through
        bad = {"messages": "not-a-list"}
        assert await h.async_pre_call_hook(None, None, bad, "completion") is bad

    asyncio.run(run_hook())

    saved = stats["chars_saved"]
    print(f"self-test OK — litellm importable: {_HAVE_LITELLM}")
    print(f"stubbed {stats['blocks']} blocks, saved {saved} chars (~{saved // 4} tokens est.)")
