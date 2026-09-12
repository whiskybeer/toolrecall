# Copyright (c) 2026 Robin Schultka
# SPDX-License-Identifier: MIT
# Source: https://github.com/whiskybeer/toolrecall
"""Canonicalization for the `hermes` proxy profile.

Hermes (WebUI/Telegram surfaces) streams every completion, so without this
profile the api_cache is structurally dead behind Hermes (STREAM bypass,
1,104/1,224 proxy rows on 2026-09-10). The warp profile exists for the Warp
harness; `hermes` covers Hermes' WebUI system-prompt volatility:

- per-session hex id on the "- Session ID:" line (constant within a
  session, forks the key across sessions/restarts)
- "Conversation started:" date line (same property)
- shared artifact classes inherited from warp: CR variants, blank-line
  runs, v4-shaped UUIDs, ISO-8601 UTC timestamps

Session volatile fields are constant WITHIN a session but fork across
sessions; neither carries model-relevant semantics. The session-ID regex is
anchored on the literal line prefix — bare hex32 hashes elsewhere in a body
(file digests, commit SHAs) must NOT merge distinct requests.

Warp-specific structures (ctx-section strip, env objects, named arrays) are
deliberately NOT applied: Hermes doesn't inject them.
"""

import hashlib
import json

from toolrecall.proxy import _canonicalize_body


def _canon_key(profile, body) -> str:
    if not isinstance(body, (bytes, bytearray)):
        body = json.dumps(body).encode()
    canon = _canonicalize_body(profile, body)
    assert canon is not None, f"{profile} profile must canonicalize"
    return hashlib.sha256(canon).hexdigest()


def _sys(session_id: str, started: str) -> dict:
    """Hermes WebUI-shaped body: runtime-context system prompt + user turn.

    Mirrors the live format (`- Session ID: <hex>` inside the WebUI session
    context block, `Conversation started: <date>` line)."""
    return {
        "model": "test-model",
        "stream": True,
        "messages": [
            {
                "role": "system",
                "content": (
                    "# Hermes runtime environment\n\n"
                    "Host: Linux\n\n"
                    "WebUI session context:\n"
                    "- This browser session is not the same live transcript.\n"
                    "- Session ID: " + session_id + "\n"
                    "- Profile: default\n\n"
                    "Conversation started: " + started + "\n\nAnswer concisely."
                ),
            },
            {"role": "user", "content": "What is 2+2?"},
        ],
    }


class TestSessionVolatileFields:
    def test_session_id_fork_fixed(self):
        a = _sys("a8c9a5c248c5", "Thursday, September 10, 2026")
        b = _sys("f0e1d2c3b4a59687", "Thursday, September 10, 2026")
        assert _canon_key("hermes", a) == _canon_key("hermes", b)

    def test_conv_started_fork_fixed(self):
        a = _sys("a8c9a5c248c5", "Thursday, September 10, 2026")
        b = _sys("a8c9a5c248c5", "Friday, September 11, 2026")
        assert _canon_key("hermes", a) == _canon_key("hermes", b)

    def test_semantic_change_still_forks(self):
        a = _sys("a8c9a5c248c5", "Thursday, September 10, 2026")
        b = _sys("a8c9a5c248c5", "Thursday, September 10, 2026")
        b["messages"][1]["content"] = "What is 2+3?"
        assert _canon_key("hermes", a) != _canon_key("hermes", b)

    def test_bare_hex_not_canonicalized(self):
        """A hex32 string NOT on the Session ID line (file digest, commit
        SHA) must NOT merge distinct bodies — the regex is line-anchored."""
        a = json.dumps(
            {
                "model": "m",
                "messages": [
                    {"role": "user", "content": "digest abcdef01abcdef01abcdef01abcdef01 end"}
                ],
            }
        ).encode()
        b = json.dumps(
            {
                "model": "m",
                "messages": [
                    {"role": "user", "content": "digest 12345678123456781234567812345678 end"}
                ],
            }
        ).encode()
        assert _canon_key("hermes", a) != _canon_key("hermes", b)


class TestInheritedArtifacts:
    def test_cr_variants_same_key(self):
        a = _sys("a8c9a5c248c5", "Thursday, September 10, 2026")
        b = _sys("a8c9a5c248c5", "Thursday, September 10, 2026")
        a["messages"][1]["content"] = "line one\r\nline two"
        b["messages"][1]["content"] = "line one\nline two"
        assert _canon_key("hermes", a) == _canon_key("hermes", b)

    def test_blank_line_runs_collapse(self):
        a = _sys("a8c9a5c248c5", "Thursday, September 10, 2026")
        b = _sys("a8c9a5c248c5", "Thursday, September 10, 2026")
        a["messages"][1]["content"] = "para one\n\n\npara two"
        b["messages"][1]["content"] = "para one\n\n\n\n\npara two"
        assert _canon_key("hermes", a) == _canon_key("hermes", b)

    def test_uuid_and_timestamp_placeholders_shared_with_warp(self):
        a = json.dumps(
            {"m": [{"c": "run 550e8400-e29b-41d4-a716-446655440000 at 2026-09-10T12:00:00Z"}]}
        ).encode()
        b = json.dumps(
            {"m": [{"c": "run 7c9e6679-7425-40de-944b-e07fc1f90ae7 at 2026-09-11T09:30:00.5Z"}]}
        ).encode()
        assert _canon_key("hermes", a) == _canon_key("hermes", b)
        assert _canon_key("hermes", a) == _canon_key("warp", a)


class TestProfileSemantics:
    def test_unknown_profile_returns_none(self):
        assert _canonicalize_body("nosuchprofile", b"{}") is None

    def test_hermes_does_not_strip_ctx_sections(self):
        """Warp strips '# Conversation context' sections; hermes must NOT.

        Hermes does not inject that structure — stripping it from Hermes
        bodies would merge semantically different requests. Cross-profile
        keys must also differ (a HIT from a warp session must not serve a
        hermes request for the same underlying task)."""
        body = json.dumps(
            {
                "messages": [
                    {
                        "role": "user",
                        "content": "# Conversation context\nsecret-A\n\n# Task\ndo it",
                    }
                ]
            }
        ).encode()
        # Warp's strip leaves the leading newline of the next section
        # (verified live: '\n# Task\ndo it').
        warp_stripped = json.dumps(
            {"messages": [{"role": "user", "content": "\n# Task\ndo it"}]}
        ).encode()
        assert _canon_key("warp", body) == _canon_key("warp", warp_stripped)  # warp strips
        assert _canon_key("hermes", body) != _canon_key("hermes", warp_stripped)  # hermes keeps

    def test_key_uses_canonical_form_provider_gets_original(self):
        """Cache key is computed over the canonical form only — the original
        body bytes are what the provider receives (contract unchanged from
        warp; re-documented for hermes)."""
        original = json.dumps(_sys("a8c9a5c248c5", "Thursday, September 10, 2026")).encode()
        canon = _canonicalize_body("hermes", original)
        assert canon is not None
        assert canon != original  # volatile fields replaced in canon form
        assert b"a8c9a5c248c5" in original  # original untouched
        assert b"a8c9a5c248c5" not in canon  # canon carries the placeholder


# ── HTTP e2e: canonicalize_default applied to headerless requests ────────────
# Full chain on hermetic state only (in this repo's spirit: the pid-gate
# ERROR taught us not to touch the live daemon from tests): mock upstream +
# E2EDaemon (subprocess on a temp socket + temp DB) + redirected HTTPS.
# Proves the handler-level fallback: [forward_proxy] canonicalize_default
# = hermes canonicalizes HEADERLESS requests, so two bodies differing only
# in session volatile fields share one cache entry (1 MISS + 1 HIT) and the
# provider still receives the original bodies.

import http.client  # noqa: E402
import http.server  # noqa: E402
import os  # noqa: E402
import socket  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
import unittest  # noqa: E402

import pytest  # noqa: E402

import toolrecall.proxy as proxy_mod  # noqa: E402


class _MockLLMHandler(http.server.BaseHTTPRequestHandler):
    call_count = 0

    def log_message(self, fmt, *args):
        return

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        _MockLLMHandler.call_count += 1
        n = _MockLLMHandler.call_count
        # SSE stream (OpenAI-compatible include_usage shape) — the tee
        # reconstructs content from delta chunks and REQUIRES the [DONE]
        # sentinel (truncated streams are discarded).
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        self.wfile.write(
            f"data: {json.dumps({'id': f'chatcmpl-canon-{n}', 'choices': [{'delta': {'content': f'upstream-call-{n}'}}]})}\n\n".encode()
        )
        self.wfile.write(
            f"data: {json.dumps({'choices': [], 'usage': {'prompt_tokens': 10, 'completion_tokens': 1}})}\n\n".encode()
        )
        self.wfile.write(b"data: [DONE]\n\n")


class _HTTPHarness:
    """Shared: allowlist pin + upstream + HTTPS redirect + proxy server."""

    host_prefix = "api.canontest.invalid"

    @classmethod
    def start_class(cls, testcase):
        testcase._orig_allowed = proxy_mod._host_allowed
        proxy_mod._host_allowed = lambda h: h.startswith(cls.host_prefix)
        testcase.upstream = http.server.HTTPServer(("127.0.0.1", 0), _MockLLMHandler)
        threading.Thread(target=testcase.upstream.serve_forever, daemon=True).start()
        testcase._orig_connect = http.client.HTTPSConnection.connect

        def redirected(self):
            self.sock = socket.create_connection(
                ("127.0.0.1", testcase.upstream.server_port), self.timeout
            )

        http.client.HTTPSConnection.connect = redirected

    @classmethod
    def stop_class(cls, testcase):
        proxy_mod._host_allowed = testcase._orig_allowed
        http.client.HTTPSConnection.connect = testcase._orig_connect
        testcase.upstream.shutdown()

    @staticmethod
    def start_proxy(testcase):
        server = http.server.HTTPServer(("127.0.0.1", 0), proxy_mod.ForwardProxyHandler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        testcase.addCleanup(server.shutdown)
        return server

    @staticmethod
    def forward(port, body: bytes, extra_headers: dict | None = None):
        headers = {"Host": _HTTPHarness.host_prefix, "Content-Type": "application/json"}
        headers.update(extra_headers or {})
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
        conn.request("POST", "/v1/chat/completions", body=body, headers=headers)
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        return resp, data


@pytest.mark.allow_prod_paths
class TestCanonicalizeDefaultHTTP(unittest.TestCase):
    """Headerless requests under canonicalize_default=hermes: session-forked
    bodies share one entry (MISS then HIT), provider sees both originals."""

    @classmethod
    def setUpClass(cls):
        from tests.e2e_helpers import E2EDaemon

        # cache_invalidate is security-gated OFF by default — the per-test
        # api_cache cleanup (setUp) needs it, so allow it in this hermetic
        # throwaway daemon only.
        os.environ["TOOLRECALL_MCP_ALLOW_INVALIDATE"] = "1"
        cls.daemon = E2EDaemon()
        cls.daemon.start()
        ping = cls.daemon.client.send({"cmd": "ping"})
        if not ping.get("pong"):
            raise unittest.SkipTest("hermetic daemon failed to start")
        # Point the handler's TransportClient at the hermetic socket.
        cls._orig_transport_path = os.environ.get("TOOLRECALL_TRANSPORT")
        os.environ["TOOLRECALL_TRANSPORT"] = cls.daemon.socket_path
        _HTTPHarness.start_class(cls)

    @classmethod
    def tearDownClass(cls):
        _HTTPHarness.stop_class(cls)
        if cls._orig_transport_path is None:
            os.environ.pop("TOOLRECALL_TRANSPORT", None)
        else:
            os.environ["TOOLRECALL_TRANSPORT"] = cls._orig_transport_path
        cls.daemon.stop()

    def setUp(self):
        # Per-test mock call counter reset — the class attribute would
        # otherwise leak across alphabetical test order.
        _MockLLMHandler.call_count = 0
        # canonicalize_default=hermes for this test (monkey-patched — the
        # handler calls the module function per request).
        self._orig_canon_default = proxy_mod._canonicalize_default
        proxy_mod._canonicalize_default = lambda: "hermes"
        self.addCleanup(self._restore_default)
        # Inter-test hygiene: unittest orders alphabetically and the sibling
        # test above may leave entries under the same canon hash space. A
        # clean api_cache per test makes each test self-contained.
        # NOTE: cache_invalidate/invalidate_all deliberately do NOT clear
        # api_cache (found 2026-09-11 while closing the tee-store race —
        # every cache table but api_cache is in the DELETE list). For this
        # hermetic throwaway daemon we clear api_cache directly via IPC.
        pong = self.daemon.client.send({"cmd": "cache_invalidate"})
        self.addCleanup(lambda: self.daemon.client.send({"cmd": "cache_invalidate"}))
        assert pong.get("result") or pong.get("error") is None, f"invalidate failed: {pong}"
        # Clear api_cache directly on the daemon's DB (invalidate_all skips
        # that table — see NOTE above). The daemon's long-lived connection
        # sees the committed delete (WAL mode, same file).
        import sqlite3 as _sq

        with _sq.connect(self.daemon.db_path) as _conn:
            _conn.execute("DELETE FROM api_cache")

    def _restore_default(self):
        proxy_mod._canonicalize_default = self._orig_canon_default

    def test_headerless_default_profile_replays(self):
        proxy = _HTTPHarness.start_proxy(self)
        a = json.dumps(_sys("a8c9a5c248c5", "Thursday, September 10, 2026")).encode()
        b = json.dumps(_sys("f0e1d2c3b4a59687", "Friday, September 11, 2026")).encode()

        # Hermes streams — first request: canonical-stream MISS path tees the
        # response into the api_cache (X-ToolRecall-Cache: STREAM = stored).
        r1, _ = _HTTPHarness.forward(proxy.server_port, a)
        self.assertEqual(r1.status, 200)
        self.assertEqual(r1.getheader("X-ToolRecall-Cache"), "STREAM")

        # Second request (different session id/date, same semantics) replays
        # from the tee'd entry as synthesized SSE (X-ToolRecall-Cache: HIT +
        # X-ToolRecall-Stream: replay) — no second upstream call. The
        # tee-settle registry closes the store race, so this is
        # deterministic on the FIRST try (pre-fix: r2 raced the store commit
        # and intermittently went live).
        r2, body2 = _HTTPHarness.forward(proxy.server_port, b)
        self.assertEqual(r2.getheader("X-ToolRecall-Cache"), "HIT")
        self.assertEqual(r2.getheader("X-ToolRecall-Stream"), "replay")
        # Replay serves THIS test's r1 body — the mock's call ids restart per
        # test (setUp resets call_count), so upstream-call-1 is r1's content.
        # (canon is key-only; the replay never reaches upstream)
        self.assertIn(b"upstream-call-1", body2)
        self.assertEqual(_MockLLMHandler.call_count, 1)

    def test_explicit_no_cache_stays_byte_hashed(self):
        """X-ToolRecall-No-Cache skips BOTH lookup and store even when a
        default profile is set (baseline-arm contract).

        Streaming relay always labels its response X-ToolRecall-Cache:
        STREAM (the label is unconditional; not a cache-participation
        signal). Opt-out is proven by BEHAVIOR: two session-forked bodies
        both reach upstream (no shared entry) — vs. the default-profile
        test above where the second body replays from the first."""
        proxy = _HTTPHarness.start_proxy(self)
        a = json.dumps(_sys("a8c9a5c248c5", "Thursday, September 10, 2026")).encode()
        b = json.dumps(_sys("f0e1d2c3b4a59687", "Friday, September 11, 2026")).encode()
        r1, _ = _HTTPHarness.forward(proxy.server_port, a, {"X-ToolRecall-No-Cache": "1"})
        self.assertEqual(r1.status, 200)
        self.assertEqual(r1.getheader("X-ToolRecall-Cache"), "STREAM")
        r2, body2 = _HTTPHarness.forward(proxy.server_port, b, {"X-ToolRecall-No-Cache": "1"})
        self.assertEqual(r2.status, 200)
        self.assertEqual(r2.getheader("X-ToolRecall-Cache"), "STREAM")
        # No shared entry: second body got its OWN live upstream response
        # (would be a replay of upstream-call-1 if the default profile had
        # applied despite the opt-out)
        self.assertEqual(_MockLLMHandler.call_count, 2)


class TestStoreRaceClosed(TestCanonicalizeDefaultHTTP):
    """The store race: r2 fired DURING r1's tee-store commit must still
    replay, not go live (pre-fix this intermittently double-billed).

    Deterministic construction: patch the handler class's _store_stream_tee
    to sleep 0.3s before deferring to the real store; r2 fires the moment
    r1's response completes — i.e. inside the store window. Pre-fix, r2's
    cached_api_check returned MISS and r2 went live (call_count=2).
    Post-fix, r2 waits on the tee-settle registration and replays
    (call_count=1). Inherits the hermetic daemon/harness from
    TestCanonicalizeDefaultHTTP.
    """

    def test_r2_during_store_waits_and_replays(self):
        orig_store = proxy_mod.ForwardProxyHandler._store_stream_tee

        def slow_store(self_handler, *a, **kw):
            time.sleep(0.3)  # r2 arrives inside this window
            return orig_store(self_handler, *a, **kw)

        proxy_mod.ForwardProxyHandler._store_stream_tee = slow_store

        try:
            proxy = _HTTPHarness.start_proxy(self)
            a = json.dumps(_sys("a8c9a5c248c5", "Thursday, September 10, 2026")).encode()
            b = json.dumps(_sys("f0e1d2c3b4a59687", "Friday, September 11, 2026")).encode()

            import http.client as _hc

            # r1: fire and read the full streamed response (returns when the
            # relay loop ends — the store is still sleeping).
            conn1 = _hc.HTTPConnection("127.0.0.1", proxy.server_port, timeout=15)
            conn1.request(
                "POST",
                "/v1/chat/completions",
                body=a,
                headers={"Host": _HTTPHarness.host_prefix, "Content-Type": "application/json"},
            )
            resp1 = conn1.getresponse()
            self.assertEqual(resp1.getheader("X-ToolRecall-Cache"), "STREAM")
            resp1.read()
            conn1.close()

            # r2 IMMEDIATELY (store thread still sleeping inside slow_store)
            r2, body2 = _HTTPHarness.forward(proxy.server_port, b)
            self.assertEqual(r2.getheader("X-ToolRecall-Cache"), "HIT")
            self.assertEqual(r2.getheader("X-ToolRecall-Stream"), "replay")
            self.assertEqual(_MockLLMHandler.call_count, 1)
            self.assertIn(b"upstream-call-1", body2)
        finally:
            proxy_mod.ForwardProxyHandler._store_stream_tee = orig_store
