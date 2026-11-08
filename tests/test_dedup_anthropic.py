"""Tests: Anthropic /v1/messages request-body dedup + prefix stability.

Covers the Claude Code path: messages-style payloads where tool output
arrives as nested ``tool_result`` content (a string or a list of text
blocks) and blocks carry ``cache_control`` markers.

The properties pinned here are the ones a provider prefix cache depends on
(Claude Code's prompt cache is exact-prefix, so a rewritten earlier block
costs a full recompute):

  P1  keep-first: the first occurrence is never rewritten
  P2  prefix stability: dedup(turn N) is a byte-prefix of dedup(turn N+1)
  P3  cache_control (and every other block key) survives stubbing
  P4  assistant content is registered but never modified
  P5  protect_last tail is never stubbed
  P6  the proxy routes /v1/messages through the same core, byte-exact off
"""

import http.client
import http.server
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

os.environ.setdefault("TOOLRECALL_CACHE_DB", "/tmp/toolrecall_dedup_anthropic_test.db")

from toolrecall.dedup import dedup_messages  # noqa: E402

FILE_TEXT = "def cache_lookup(key):\n    return _TABLE.get(key)\n" * 100  # ~4.6K chars


def _anthropic_messages(dups: bool = True) -> list:
    """Anthropic-shaped history: tool output inside tool_result blocks.

    Indices: 0 user, 1 assistant(tool_use), 2 user(tool_result: big file),
    3 assistant(text), 4 user(tool_result: same file again), 5 assistant,
    6 user — so the duplicate at 4 sits outside protect_last=2.
    """
    second = FILE_TEXT if dups else FILE_TEXT + "# different\n"
    return [
        {"role": "user", "content": [{"type": "text", "text": "Fix the cache bug."}]},
        {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "toolu_1",
                    "name": "Read",
                    "input": {"file_path": "cache.py"},
                }
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "toolu_1",
                    "content": [{"type": "text", "text": FILE_TEXT}],
                    "cache_control": {"type": "ephemeral"},
                }
            ],
        },
        {"role": "assistant", "content": [{"type": "text", "text": "I see the bug."}]},
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "toolu_2", "content": second}],
        },
        {"role": "assistant", "content": [{"type": "text", "text": "Patching."}]},
        {"role": "user", "content": [{"type": "text", "text": "Summarize."}]},
    ]


class TestAnthropicShapeDedup(unittest.TestCase):
    def test_tool_result_nested_text_block_is_stubbed_keep_first(self):
        out, stats = dedup_messages(_anthropic_messages(), min_chars=800, protect_last=2)
        self.assertEqual(stats["blocks"], 1)
        # P1: first occurrence byte-intact, including its cache_control marker
        first = out[2]["content"][0]
        self.assertEqual(first["content"], [{"type": "text", "text": FILE_TEXT}])
        self.assertEqual(first["cache_control"], {"type": "ephemeral"})
        # the duplicate's inner text was replaced by the stub
        dup = out[4]["content"][0]
        self.assertEqual(dup["type"], "tool_result")
        self.assertEqual(dup["tool_use_id"], "toolu_2")
        self.assertTrue(dup["content"].startswith("[toolrecall-dedup]"))

    def test_tool_result_string_content_is_stubbed(self):
        msgs = _anthropic_messages()
        # same payload, but the duplicate carries a plain-string content
        msgs[4]["content"][0]["content"] = FILE_TEXT
        out, stats = dedup_messages(msgs, min_chars=800, protect_last=2)
        self.assertEqual(stats["blocks"], 1)
        self.assertTrue(out[4]["content"][0]["content"].startswith("[toolrecall-dedup]"))
        self.assertEqual(out[2]["content"][0]["content"], [{"type": "text", "text": FILE_TEXT}])

    def test_assistant_content_registered_but_never_modified(self):
        big = "assistant-authored block\n" * 200
        msgs = [
            {"role": "assistant", "content": [{"type": "text", "text": big}]},
            {"role": "user", "content": [{"type": "text", "text": "ok"}]},
            {"role": "user", "content": [{"type": "text", "text": big}]},
            {"role": "user", "content": [{"type": "text", "text": "tail"}]},
        ]
        out, stats = dedup_messages(msgs, min_chars=800, protect_last=1)
        self.assertEqual(stats["blocks"], 1)
        # P4: the assistant block is untouched; the later user copy is stubbed
        self.assertEqual(out[0]["content"][0]["text"], big)
        self.assertTrue(out[2]["content"][0]["text"].startswith("[toolrecall-dedup]"))

    def test_protect_last_never_stubs_tail(self):
        msgs = _anthropic_messages()
        # duplicate at index 4 of 5... shift it into the protected tail
        out, stats = dedup_messages(msgs, min_chars=800, protect_last=3)
        self.assertEqual(stats["blocks"], 0)
        self.assertEqual(out[4]["content"][0]["content"], FILE_TEXT)

    def test_cache_control_survives_on_untouched_blocks(self):
        msgs = _anthropic_messages(dups=False)
        out, stats = dedup_messages(msgs, min_chars=800, protect_last=2)
        self.assertEqual(stats["blocks"], 0)
        self.assertEqual(out[2]["content"][0]["cache_control"], {"type": "ephemeral"})

    def test_deterministic(self):
        a, sa = dedup_messages(_anthropic_messages(), min_chars=800, protect_last=2)
        b, sb = dedup_messages(_anthropic_messages(), min_chars=800, protect_last=2)
        self.assertEqual(a, b)
        self.assertEqual(sa, sb)

    def test_input_not_mutated(self):
        msgs = _anthropic_messages()
        before = json.dumps(msgs, sort_keys=True)
        dedup_messages(msgs, min_chars=800, protect_last=2)
        self.assertEqual(json.dumps(msgs, sort_keys=True), before)

    def test_short_blocks_untouched(self):
        msgs = _anthropic_messages()
        out, stats = dedup_messages(msgs, min_chars=100000, protect_last=2)
        self.assertEqual(stats["blocks"], 0)


class TestPrefixStability(unittest.TestCase):
    """P2 — the property Claude Code's exact-prefix prompt cache needs."""

    def _turn_growth(self, shape: str) -> None:
        if shape == "anthropic":
            turn1 = _anthropic_messages(dups=False)[:5]
            turn2 = turn1 + _anthropic_messages(dups=True)[4:]
        else:
            turn1 = [
                {"role": "system", "content": "sys"},
                {"role": "user", "content": "go"},
                {"role": "tool", "content": FILE_TEXT},
                {"role": "user", "content": "again"},
            ]
            turn2 = turn1 + [
                {"role": "tool", "content": FILE_TEXT},
                {"role": "assistant", "content": "ok"},
                {"role": "user", "content": "sum"},
            ]
        out1, _ = dedup_messages(json.loads(json.dumps(turn1)), min_chars=800, protect_last=2)
        out2, stats2 = dedup_messages(json.loads(json.dumps(turn2)), min_chars=800, protect_last=2)
        # the new turn really did stub something (so this is not a vacuous pass)
        self.assertGreaterEqual(stats2["blocks"], 1, f"{shape}: expected a stub in turn 2")
        # every message that existed in turn 1 is byte-identical in turn 2
        self.assertEqual(out2[: len(out1)], out1, f"{shape}: prefix changed across turns")

    def test_prefix_stable_anthropic(self):
        self._turn_growth("anthropic")

    def test_prefix_stable_openai(self):
        self._turn_growth("openai")


class TestProxyDedupPathGate(unittest.TestCase):
    """P6a — the proxy's dedup gate covers both message-carrying paths."""

    def test_dedup_path_predicate(self):
        import toolrecall.proxy as proxy_mod

        for path in (
            "/v1/chat/completions",
            "/v1/messages",
            "/v1/messages?beta=true",
            "/chat/completions",
        ):
            self.assertTrue(proxy_mod._is_dedup_path(path), path)
        for path in (
            "/v1/messages/count_tokens",
            "/v1/embeddings",
            "/v1/models",
            "/v1/complete",
        ):
            self.assertFalse(proxy_mod._is_dedup_path(path), path)


class _Upstream(http.server.BaseHTTPRequestHandler):
    """Mock Anthropic upstream: records the body it actually received."""

    last_body = None

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        _Upstream.last_body = self.rfile.read(n)
        payload = json.dumps(
            {
                "id": "msg_mock",
                "type": "message",
                "content": [{"type": "text", "text": "ok"}],
                "usage": {"input_tokens": 10, "output_tokens": 1},
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, fmt, *args):  # silence
        pass


class TestProxyMessagesHTTP(unittest.TestCase):
    """P6b — end-to-end: POST /v1/messages through ForwardProxyHandler."""

    @classmethod
    def setUpClass(cls):
        import toolrecall.proxy as proxy_mod

        # Pin the usage log to a temp file. ForwardProxyHandler appends one row
        # per request to ~/.toolrecall/proxy_usage.csv — the PRODUCTION
        # billed-truth file. Unpinned, this class leaks fake rows into it
        # (observed: 8 rows, host api.anthropictest.invalid). Same rule as
        # tests/test_usage_dedup_column.py.
        cls._orig_usage_path = proxy_mod._USAGE_LOG_PATH
        cls._usage_dir = tempfile.mkdtemp(prefix="tr_anthropic_usage_")
        proxy_mod._USAGE_LOG_PATH = os.path.join(cls._usage_dir, "usage.csv")

        cls._orig_allowed = proxy_mod._host_allowed
        proxy_mod._host_allowed = lambda h: h.startswith("api.anthropictest.invalid")
        cls.upstream = http.server.HTTPServer(("127.0.0.1", 0), _Upstream)
        threading.Thread(target=cls.upstream.serve_forever, daemon=True).start()
        cls._orig_connect = http.client.HTTPSConnection.connect

        def redirected(self):
            self.sock = socket.create_connection(
                ("127.0.0.1", cls.upstream.server_port), self.timeout
            )

        http.client.HTTPSConnection.connect = redirected

    @classmethod
    def tearDownClass(cls):
        import toolrecall.proxy as proxy_mod

        proxy_mod._host_allowed = cls._orig_allowed
        http.client.HTTPSConnection.connect = cls._orig_connect
        proxy_mod._USAGE_LOG_PATH = cls._orig_usage_path
        shutil.rmtree(cls._usage_dir, ignore_errors=True)
        cls.upstream.shutdown()

    def setUp(self):
        for k in ("TOOLRECALL_FORWARD_DEDUP", "TOOLRECALL_DEDUP_MIN_CHARS"):
            os.environ.pop(k, None)
        os.environ["TOOLRECALL_FORWARD_DEDUP"] = "false"
        _Upstream.last_body = None

    def tearDown(self):
        for k in ("TOOLRECALL_FORWARD_DEDUP", "TOOLRECALL_DEDUP_MIN_CHARS"):
            os.environ.pop(k, None)

    def _proxy(self):
        import toolrecall.proxy as proxy_mod

        server = http.server.HTTPServer(("127.0.0.1", 0), proxy_mod.ForwardProxyHandler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        return server

    def _post(self, port, body: bytes):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request(
            "POST",
            "/v1/messages",
            body=body,
            headers={
                "Host": "api.anthropictest.invalid",
                "Content-Type": "application/json",
                "anthropic-version": "2023-06-01",
            },
        )
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        return resp, data

    def _envelope(self) -> bytes:
        return json.dumps(
            {"model": "claude-test", "max_tokens": 1024, "messages": _anthropic_messages()}
        ).encode()

    def test_flag_off_is_byte_exact(self):
        p = self._proxy()
        body = self._envelope()
        resp, _ = self._post(p.server_port, body)
        self.assertEqual(resp.status, 200)
        self.assertEqual(_Upstream.last_body, body)

    def test_flag_on_stubs_duplicate_tool_result(self):
        os.environ["TOOLRECALL_FORWARD_DEDUP"] = "true"
        p = self._proxy()
        body = self._envelope()
        resp, _ = self._post(p.server_port, body)
        self.assertEqual(resp.status, 200)
        received = json.loads(_Upstream.last_body.decode())
        self.assertEqual(len(received["messages"]), 7)
        first = received["messages"][2]["content"][0]
        dup = received["messages"][4]["content"][0]
        # keep-first + marker preservation survive the full proxy round trip
        self.assertEqual(first["content"], [{"type": "text", "text": FILE_TEXT}])
        self.assertEqual(first["cache_control"], {"type": "ephemeral"})
        self.assertTrue(dup["content"].startswith("[toolrecall-dedup]"))


class TestProtectedTailVsCache(unittest.TestCase):
    """The protected tail interacts with a provider prefix cache (P2, honest form).

    Claude Code's repeated file almost always lands in the NEWEST ``tool_result``
    — i.e. inside ``protect_last``. On the turn it arrives it therefore goes out
    in full (and gets cached: Claude Code's cache breakpoint sits on the last
    message). One turn later that message leaves the protected tail and is
    finally stubbed — which rewrites bytes the provider already cached, so the
    prefix stops matching from that message on and everything after it is
    re-written (~1.25x) instead of read (~0.1x).

    So "keep-first keeps the byte-prefix stable" holds only for duplicates that
    are stubbed on their FIRST send. This class pins the difference: it is why
    the Claude Code arm is worth running with ``TOOLRECALL_DEDUP_PROTECT_LAST=0``
    — keep-first already keeps the full copy in the request, so nothing is lost.
    """

    @staticmethod
    def _turns(n_turns: int) -> list[list[dict]]:
        """Cumulative contexts: every turn appends a FRESH copy of FILE_TEXT."""
        msgs = [
            {"role": "user", "content": [{"type": "text", "text": "start"}]},
            {
                "role": "assistant",
                "content": [{"type": "tool_use", "id": "t0", "name": "Read", "input": {}}],
            },
        ]
        turns = []
        for i in range(n_turns):
            msgs = msgs + [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": f"t{i + 1}",
                            "content": [{"type": "text", "text": FILE_TEXT}],
                        }
                    ],
                },
                {"role": "assistant", "content": [{"type": "text", "text": f"ok {i}"}]},
            ]
            turns.append(list(msgs))
        return turns

    @staticmethod
    def _ser(msgs: list) -> list[str]:
        return [json.dumps(m, sort_keys=True) for m in msgs]

    def _run(self, turns: list, protect_last: int):
        outs, stats = [], []
        for msgs in turns:
            deduped, st = dedup_messages(msgs, protect_last=protect_last)
            outs.append(self._ser(deduped))
            stats.append(st)
        return outs, stats

    def test_default_tail_defers_the_stub_then_invalidates_the_prefix(self):
        """protect_last=2: nothing stubbed while it sits in the tail; then a rewrite."""
        outs, stats = self._run(self._turns(3), 2)

        self.assertEqual(stats[0]["blocks"], 0, "turn 1: duplicate is in the tail → full send")
        self.assertEqual(stats[1]["blocks"], 0, "turn 2: first copy kept + newest still protected")
        self.assertGreaterEqual(
            stats[2]["blocks"], 1, "turn 3: the copy that aged out of the tail is stubbed"
        )
        self.assertNotEqual(
            outs[2][: len(outs[1])],
            outs[1],
            "prefix changed at turn 3 → bytes already sent and cached got rewritten",
        )

    def test_protect_last_zero_stubs_on_first_send_and_keeps_the_prefix(self):
        """protect_last=0: stub lands on the duplicate's FIRST send → prefix stays stable."""
        outs, stats = self._run(self._turns(3), 0)

        self.assertEqual(stats[0]["blocks"], 0, "first occurrence is never stubbed")
        self.assertGreaterEqual(stats[1]["blocks"], 1, "turn 2: stubbed on its first send")
        self.assertEqual(
            outs[2][: len(outs[1])],
            outs[1],
            "prefix stable: no already-cached byte is rewritten across turns",
        )


if __name__ == "__main__":
    unittest.main()
