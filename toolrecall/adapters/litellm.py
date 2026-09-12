# Copyright (c) 2026 Robin Schultka
# SPDX-License-Identifier: MIT
# Source: https://github.com/whiskybeer/toolrecall
"""
toolrecall.adapters.litellm — request-level duplicate-content dedup for LiteLLM Proxy.

Plugs into LiteLLM's ``async_pre_call_hook`` (fires just before the request is sent
to the provider). Scans ``data["messages"]`` for large text blocks that appear more
than once in the same request — typically repeated file reads / tool results in agent
loops — and replaces every duplicate AFTER the first occurrence with a short stub.

The dedup algorithm lives in ``toolrecall.dedup`` (transport-neutral core); this
module is the LiteLLM binding of it. The forward proxy binds the same core
(``[forward_proxy] dedup = true``) — one algorithm, one test surface.

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

import base64
import logging
import os
from typing import Any

from toolrecall.dedup import (  # noqa: F401 — public API re-exports
    DEFAULT_STUB_ROLES,
    IMAGE_STUB_TEMPLATE,
    STUB_TEMPLATE,
    _decode_data_url,
    _digest,
    dedup_messages,
)

try:  # pragma: no cover — import guard so the module also loads outside litellm
    from litellm.integrations.custom_logger import CustomLogger

    _HAVE_LITELLM = True
except Exception:  # noqa: BLE001

    class CustomLogger:  # type: ignore[no-redef]
        """Fallback shim so the dedup logic is importable/testable without litellm."""

    _HAVE_LITELLM = False

log = logging.getLogger("toolrecall.dedup")


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
        # Experimental (v1, default off): dedup data-URL image parts.
        self.media = os.getenv("TOOLRECALL_DEDUP_MEDIA", "").lower() in (
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
                media=self.media,
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

    # --- Media (experimental, data-URL images) ---
    PNG1 = b"\x89PNG\r\n\x1a\n" + b"A" * 1200  # decoded size > min_chars floor
    PNG2 = b"\x89PNG\r\n\x1a\n" + b"B" * 1200
    url1 = "data:image/png;base64," + base64.b64encode(PNG1).decode()
    url1_relabeled = "data:image/jpeg;base64," + base64.b64encode(PNG1).decode()
    url2 = "data:image/png;base64," + base64.b64encode(PNG2).decode()

    def media_convo():
        return [
            {"role": "system", "content": "You are a browser agent."},
            {"role": "user", "content": "Check the page."},
            {
                "role": "tool",
                "content": [
                    {"type": "text", "text": FILE_A},
                    {"type": "image_url", "image_url": {"url": url1, "detail": "high"}},
                ],
            },
            {"role": "user", "content": "Again."},
            {
                "role": "tool",
                "content": [
                    {"type": "image_url", "image_url": {"url": url1}},  # dup -> stub
                ],
            },
            {
                "role": "tool",
                "content": [
                    # same bytes, relabeled mime + different wrap -> same identity
                    {"type": "image_url", "image_url": {"url": url1_relabeled}},
                ],
            },
            {"role": "user", "content": "New page."},
            {
                "role": "tool",
                "content": [
                    {"type": "image_url", "image_url": {"url": url2}},  # distinct -> kept
                ],
            },
            {"role": "user", "content": "Summarize."},
        ]

    mout, mstats = dedup_messages(media_convo(), min_chars=800, protect_last=2, media=True)
    assert mstats["blocks"] == 2, mstats
    assert mout[2]["content"][1]["image_url"]["url"] == url1  # first kept in full
    assert "detail" in mout[2]["content"][1]["image_url"]  # siblings preserved
    # stubbed dups become short TEXT parts (providers reject stub text in
    # image_url.url with 400 "Invalid URL format" — gate-probe verified)
    s4 = mout[4]["content"][0]
    assert s4["type"] == "text" and s4["text"].startswith("[toolrecall-dedup]")
    assert "message 2" in s4["text"]
    s5 = mout[5]["content"][0]
    assert s5["type"] == "text" and s5["text"].startswith("[toolrecall-dedup]")
    assert mout[7]["content"][0]["image_url"]["url"] == url2  # distinct image kept
    assert media_convo()[2]["content"][1]["image_url"]["url"] == url1  # input untouched

    # media=False (default): identical image parts pass through untouched
    mout_off, mstats_off = dedup_messages(media_convo(), min_chars=800, protect_last=2)
    assert mstats_off["blocks"] == 0
    assert mout_off[4]["content"][0]["image_url"]["url"] == url1

    # https:// refs are never deduped (identity is the URL, not content)
    https_convo = [
        {
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": "https://x/img.png"}},
            ],
        },
        {
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": "https://x/img.png"}},
            ],
        },
    ]
    hout, hstats = dedup_messages(https_convo, min_chars=1, protect_last=0, media=True)
    assert hstats["blocks"] == 0
    assert hout[1]["content"][0]["image_url"]["url"] == "https://x/img.png"

    # corrupt base64 -> left alone, no crash
    bad_convo = [
        {
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,!!notb64!!"}},
            ],
        }
    ]
    bout, bstats = dedup_messages(bad_convo, min_chars=1, protect_last=0, media=True)
    assert bstats["blocks"] == 0
    assert bout[0]["content"][0]["image_url"]["url"] == "data:image/png;base64,!!notb64!!"

    # armor: with protect_last=2, only the tail's last TWO messages are
    # guaranteed full copies — earlier dups stub, the last two never do
    tail_convo = [
        {"role": "user", "content": [{"type": "image_url", "image_url": {"url": url1}}]},
        {"role": "user", "content": [{"type": "image_url", "image_url": {"url": url1}}]},
        {"role": "user", "content": [{"type": "image_url", "image_url": {"url": url1}}]},
        {"role": "user", "content": [{"type": "image_url", "image_url": {"url": url1}}]},
    ]
    tout, tstats = dedup_messages(tail_convo, min_chars=1, protect_last=2, media=True)
    assert tout[0]["content"][0]["image_url"]["url"] == url1  # first occurrence kept
    assert tout[1]["content"][0].get("text", "").startswith("[toolrecall-dedup]")
    assert tout[2]["content"][0]["image_url"]["url"] == url1  # protected tail
    assert tout[3]["content"][0]["image_url"]["url"] == url1  # protected tail
    assert tstats["blocks"] == 1, tstats

    # handler: media flag from env, off by default
    async def run_media_hook():
        data = {"messages": media_convo(), "model": "gpt-x"}
        h_off = ToolRecallDedupHandler()
        await h_off.async_pre_call_hook(None, None, data, "acompletion")
        assert data["messages"][4]["content"][0]["image_url"]["url"] == url1
        os.environ["TOOLRECALL_DEDUP_MEDIA"] = "1"
        try:
            h_on = ToolRecallDedupHandler()
            assert h_on.media is True
            data2 = {"messages": media_convo(), "model": "gpt-x"}
            await h_on.async_pre_call_hook(None, None, data2, "acompletion")
            s = data2["messages"][4]["content"][0]
            assert s["type"] == "text" and s["text"].startswith("[toolrecall-dedup]")
        finally:
            del os.environ["TOOLRECALL_DEDUP_MEDIA"]

    asyncio.run(run_media_hook())

    # Parity: the core module in toolrecall.dedup must behave identically
    # (it IS the same function — this guards against accidental divergence)
    from toolrecall.dedup import dedup_messages as core_dedup

    c_out, c_stats = core_dedup(convo(), min_chars=800, protect_last=2)
    assert c_out == out and c_stats == stats

    print("self-test OK (text + media)")
