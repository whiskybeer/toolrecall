# Copyright (c) 2026 Robin Schultka
# SPDX-License-Identifier: MIT
# Source: https://github.com/whiskybeer/toolrecall
"""Empty-stream cache guard — no degenerate responses enter api_cache.

Measured live 2026-09-12 through the prod proxy (:8569): a z-ai/glm-flash
stream that emitted only ~107 reasoning tokens (no content, no tool_calls)
then a well-formed [DONE] was tee-stored under its canonical hash. Every
subsequent identical request then replayed the same useless stream
(ct=0 HITs, hash 39ae2df2…×5 in one minute) — the retry loop kept hitting
its own poisoned entry until the harness gave up with an apperror.

66 such HIT events lifetime, always in bursts (Sep 4/6/8/11/12). The only
store guard was "[DONE] present" — a degenerate but well-formed stream
passes it. Fix: store only streams that carried client-visible output
(content or tool_calls deltas); reasoning-only streams are discarded so
the retry reaches the provider fresh. Reasoning deltas deliberately do
NOT satisfy the guard: replaying them renders nothing user-visible (the
measured failure) — the client needs a fresh provider turn.
"""

import json
import sys
import unittest
from unittest import mock

sys.path.insert(0, __file__.rsplit("/", 2)[0])

from toolrecall import proxy as proxy_mod  # noqa: E402


def _handler():
    """A handler instance without touching sockets (binds nothing)."""
    return object.__new__(proxy_mod.ForwardProxyHandler)


def _sse(*objs) -> list[bytes]:
    """SSE chunks as the tee buffer accumulates them, plus the [DONE]
    sentinel the store guard requires (truncated streams are discarded)."""
    out = [f"data: {json.dumps(obj)}\n\n".encode() for obj in objs]
    out.append(b"data: [DONE]\n\n")
    return out


_CHUNK = {"id": "chatcmpl-tee", "choices": [{"delta": {"content": "x"}}]}
_USAGE = {"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 5}}
_TOOLCALL = {
    "id": "t1",
    "choices": [
        {
            "delta": {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "call_1",
                        "function": {"name": "read", "arguments": "{}"},
                    }
                ]
            }
        }
    ],
}
_REASONING = {"id": "c1", "choices": [{"delta": {"reasoning_content": " think"}}]}


class TestEmptyStreamGuard(unittest.TestCase):
    """_store_stream_tee: content-only (stored), reasoning-only (discarded),
    tool_call-only (stored), truncation (discarded), store failure (soft)."""

    def _store(self, chunks):
        h = _handler()
        h._client = mock.Mock()  # stored paths call send(); discard paths must not
        with mock.patch.object(proxy_mod, "log") as mlog:
            h._store_stream_tee(list(chunks), "hash-x", b"req", "h.invalid", "/v1/x")
            return mlog, h._client.send

    def test_content_only_stream_still_stored(self):
        """Regression: the guard must not over-correct and drop normal
        streams (the 2026-09-10 inflation class taught us that over-eager
        guards are worse than the defect they guard against)."""
        _, send = self._store(_sse(_CHUNK, _USAGE))
        self.assertTrue(send.called)

    def test_reasoning_only_stream_discarded(self):
        """The measured prod failure: z-ai/glm-flash emitted ~107 reasoning
        tokens, no content/tool_calls, clean [DONE] — and got cached,
        poisoning every retry (5× ct=0 HIT burst, hash 39ae2df2…)."""
        m, send = self._store(_sse(_REASONING, _USAGE))
        self.assertFalse(send.called)
        self.assertIn("discarding empty stream", str(m.warning.call_args))

    def test_tool_call_only_stream_stored(self):
        """Tool-call turns are the norm for agent harnesses (see the
        accumulator comment in _store_stream_tee) — must remain cacheable."""
        _, send = self._store(_sse(_TOOLCALL, _USAGE))
        self.assertTrue(send.called)

    def test_truncated_still_discarded(self):
        m, send = self._store([f"data: {json.dumps(_CHUNK)}\n\n".encode()])  # no [DONE]
        self.assertFalse(send.called)
        self.assertIn("discarding truncated stream", str(m.warning.call_args))

    def test_store_failure_soft(self):
        """Daemon transport failure stays non-fatal (client already has the
        response; the store is best-effort)."""
        h = _handler()
        h._client = mock.Mock()
        h._client.send.side_effect = OSError("socket gone")
        with mock.patch.object(proxy_mod, "log") as mlog:
            h._store_stream_tee(_sse(_CHUNK, _USAGE), "hash-y", b"req", "h.invalid", "/v1/x")
        self.assertIn("Stream tee/store failed", str(mlog.warning.call_args))


if __name__ == "__main__":
    unittest.main()
