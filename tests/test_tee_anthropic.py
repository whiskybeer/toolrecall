# Copyright (c) 2026 Robin Schultka
# SPDX-License-Identifier: MIT
# Source: https://github.com/whiskybeer/toolrecall
"""Stream tee + replay for Anthropic Messages SSE (Claude Code traffic).

Measured 2026-11-07: Anthropic streams end with a ``message_stop`` event and
never send ``data: [DONE]``, so the tee discarded every Claude Code stream as
"truncated" — 0 of 9 streamed calls stored, replay never fired. The October
Claude Code replay results came from non-streaming fallback retries after a
since-fixed stream-relay break, not from the stream cache.
"""

import json
import sys
import unittest
from unittest import mock

sys.path.insert(0, __file__.rsplit("/", 2)[0])

from toolrecall import proxy as proxy_mod  # noqa: E402


def _ev(name, obj) -> bytes:
    return f"event: {name}\ndata: {json.dumps(obj)}\n\n".encode()


_START = {
    "type": "message_start",
    "message": {
        "id": "msg_1",
        "model": "claude-x",
        "usage": {
            "input_tokens": 3,
            "cache_read_input_tokens": 1000,
            "cache_creation_input_tokens": 200,
            "output_tokens": 1,
        },
    },
}
_TEXT_START = {
    "type": "content_block_start",
    "index": 0,
    "content_block": {"type": "text", "text": ""},
}
_TEXT = {
    "type": "content_block_delta",
    "index": 0,
    "delta": {"type": "text_delta", "text": "hello"},
}
_TOOL_START = {
    "type": "content_block_start",
    "index": 0,
    "content_block": {"type": "tool_use", "id": "toolu_1", "name": "Read", "input": {}},
}
_TOOL_DELTA = {
    "type": "content_block_delta",
    "index": 0,
    "delta": {"type": "input_json_delta", "partial_json": '{"file_path": "a.py"}'},
}
_STOP_BLOCK = {"type": "content_block_stop", "index": 0}
_MSG_DELTA = {
    "type": "message_delta",
    "delta": {"stop_reason": "end_turn"},
    "usage": {"output_tokens": 5},
}
_MSG_STOP = {"type": "message_stop"}
_THINK = {
    "type": "content_block_delta",
    "index": 0,
    "delta": {"type": "thinking_delta", "thinking": "hm"},
}


def _stream(*events, stop=True):
    names = {
        "message_start": "message_start",
        "content_block_start": "content_block_start",
        "content_block_delta": "content_block_delta",
        "content_block_stop": "content_block_stop",
        "message_delta": "message_delta",
        "error": "error",
    }
    out = [_ev(names.get(e["type"], e["type"]), e) for e in events]
    if stop:
        out.append(_ev("message_stop", _MSG_STOP))
    return out


class TestAnthropicTee(unittest.TestCase):
    def _store(self, chunks):
        h = object.__new__(proxy_mod.ForwardProxyHandler)
        h._client = mock.Mock()
        with mock.patch.object(proxy_mod, "log") as mlog:
            h._store_stream_tee(list(chunks), "hash-a", b"req", "api.anthropic.com", "/v1/messages")
            return mlog, h._client.send

    def test_text_stream_stored(self):
        _, send = self._store(_stream(_START, _TEXT_START, _TEXT, _STOP_BLOCK, _MSG_DELTA))
        self.assertTrue(send.called)
        stored = send.call_args[0][0]["response_body"]
        self.assertIn("message_stop", stored)  # raw stream stored verbatim

    def test_tool_use_only_stream_stored(self):
        _, send = self._store(_stream(_START, _TOOL_START, _TOOL_DELTA, _STOP_BLOCK, _MSG_DELTA))
        self.assertTrue(send.called)

    def test_truncated_anthropic_stream_discarded(self):
        m, send = self._store(_stream(_START, _TEXT_START, _TEXT, stop=False))
        self.assertFalse(send.called)
        self.assertIn("discarding truncated stream", str(m.warning.call_args))

    def test_error_event_never_cached(self):
        err = {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}}
        m, send = self._store(_stream(_START, _TEXT_START, _TEXT, err))
        self.assertFalse(send.called)
        self.assertIn("error event", str(m.warning.call_args))

    def test_store_failure_is_not_logged_as_stored(self):
        h = object.__new__(proxy_mod.ForwardProxyHandler)
        h._client = mock.Mock()
        h._client.send.return_value = {"error": "daemon_unavailable"}
        with mock.patch.object(proxy_mod, "log") as mlog:
            h._store_stream_tee(
                _stream(_START, _TEXT_START, _TEXT), "h", b"r", "api.anthropic.com", "/v1/messages"
            )
        self.assertIn("store failed", str(mlog.warning.call_args))
        self.assertFalse(mlog.info.called)

    def test_thinking_only_stream_discarded(self):
        m, send = self._store(_stream(_START, _THINK, _MSG_DELTA))
        self.assertFalse(send.called)
        self.assertIn("discarding empty stream", str(m.warning.call_args))


class TestAnthropicReplay(unittest.TestCase):
    def test_sse_complete(self):
        raw = b"".join(_stream(_START, _TEXT)).decode()
        self.assertTrue(proxy_mod._sse_complete(raw))
        self.assertFalse(
            proxy_mod._sse_complete(b"".join(_stream(_START, _TEXT, stop=False)).decode())
        )
        self.assertTrue(proxy_mod._sse_complete("data: {}\n\ndata: [DONE]\n\n"))

    def test_replay_serves_anthropic_stream(self):
        raw = b"".join(_stream(_START, _TEXT_START, _TEXT, _STOP_BLOCK, _MSG_DELTA)).decode()
        h = object.__new__(proxy_mod.ForwardProxyHandler)
        h.send_response = mock.Mock()
        h.send_header = mock.Mock()
        h.end_headers = mock.Mock()
        h.wfile = mock.Mock()
        n = h._replay_stream_from_cache({"body": raw}, "api.anthropic.com", "/v1/messages")
        self.assertEqual(n, 1203)  # input + cache read + cache write from message_start
        h.send_response.assert_called_with(200)
        self.assertEqual(h.wfile.write.call_args[0][0], raw.encode())


if __name__ == "__main__":
    unittest.main()
