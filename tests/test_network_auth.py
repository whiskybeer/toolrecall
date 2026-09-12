"""Tests for network_auth (A6 Task 2.2) — HMAC-SHA256 challenge-response.

Pure-stdlib protocol surface: nonce generation, response computation,
constant-time verification, nonce-replay cache, wire framing helpers,
and the timeout-bounded server-side handshake used by the TCP listener.
"""

import hmac as hmac_mod
import socket
import threading
import time

import pytest

from toolrecall import network_auth as na


def _listen():
    """Bind a loopback listener on an ephemeral port; return (socket, port)."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(4)
    return srv, srv.getsockname()[1]


class TestNonce:
    def test_nonce_is_32_bytes(self):
        assert len(na.new_nonce()) == 32

    def test_nonces_are_unique(self):
        seen = {na.new_nonce() for _ in range(1000)}
        assert len(seen) == 1000


class TestResponse:
    def test_response_is_hmac_of_token_and_nonce(self):
        token = b"t0k3n"
        nonce = b"\x01" * 32
        expected = hmac_mod.new(token, nonce, "sha256").digest()
        assert na.response_for(token, nonce) == expected

    def test_response_is_32_bytes(self):
        assert len(na.response_for(b"t", na.new_nonce())) == 32

    def test_different_tokens_different_responses(self):
        nonce = na.new_nonce()
        assert na.response_for(b"a", nonce) != na.response_for(b"b", nonce)

    def test_str_token_coerced_to_bytes(self):
        nonce = na.new_nonce()
        assert na.response_for("tok", nonce) == na.response_for(b"tok", nonce)


class TestVerification:
    def test_roundtrip_ok(self):
        token = b"correct horse"
        cache = na.NonceCache()
        nonce = na.new_nonce()
        resp = na.response_for(token, nonce)
        client_id = na.verify_response(token, nonce, resp, cache)
        assert client_id is not None

    def test_wrong_token_rejected(self):
        cache = na.NonceCache()
        nonce = na.new_nonce()
        resp = na.response_for(b"correct", nonce)
        assert na.verify_response(b"wrong", nonce, resp, cache) is False

    def test_garbage_response_rejected(self):
        cache = na.NonceCache()
        assert na.verify_response(b"t", na.new_nonce(), b"\x00" * 32, cache) is False

    def test_wrong_length_response_rejected(self):
        cache = na.NonceCache()
        assert na.verify_response(b"t", na.new_nonce(), b"short", cache) is False

    def test_constant_time_compare_used(self):
        """The comparison must go through hmac.compare_digest (timing)."""
        src = __import__("inspect").getsource(na.verify_response)
        assert "compare_digest" in src

    def test_verify_returns_token_id_when_token_is_id_scoped(self):
        """verify_response takes the *expected* token; identity binding is the
        listener's job (it knows which token-id matched). Verified implicitly:
        the function returns a truthy marker only on a full digest match."""
        token = b"team-token-1"
        cache = na.NonceCache()
        nonce = na.new_nonce()
        assert na.verify_response(token, nonce, na.response_for(token, nonce), cache)
        assert not na.verify_response(token, nonce, na.response_for(b"other", nonce), cache)


class TestNonceCache:
    def test_replay_rejected(self):
        cache = na.NonceCache()
        nonce = na.new_nonce()
        resp = na.response_for(b"t", nonce)
        assert na.verify_response(b"t", nonce, resp, cache) is not False
        # Same (nonce, resp) pair again = replay
        assert na.verify_response(b"t", nonce, resp, cache) is False

    def test_nonce_never_repeats(self):
        """A fresh nonce works; a nonce already spent is refused."""
        cache = na.NonceCache(maxsize=128)
        spent = set()
        for _ in range(500):
            n = na.new_nonce()
            assert n not in spent or cache.seen(n)
            if cache.claim(n):
                assert not cache.claim(n)  # second claim fails
            spent.add(n)

    def test_cache_eviction_allows_old_nonce_after_wraparound(self):
        """maxsize bounds memory; entries evicted LRU-style. Documented
        trade-off: an attacker who can force maxsize+1 handshakes within the
        auth window can replay — mitigated by the 3s auth window."""
        cache = na.NonceCache(maxsize=8)
        nonce = na.new_nonce()
        assert cache.claim(nonce) is True
        for _ in range(16):  # push it out of the LRU window
            cache.claim(na.new_nonce())
        assert cache.seen(nonce) is False

    def test_claim_returns_bool(self):
        cache = na.NonceCache()
        assert isinstance(cache.claim(na.new_nonce()), bool)


class TestFraming:
    """Auth handshake framing: nonce is raw 32 bytes; response is raw 32
    bytes; a 1-byte protocol tag prefixes the client response so the server
    can distinguish handshake bytes from early framed traffic."""

    def test_client_greeting_layout(self):
        greeting = na.client_greeting(b"tok", na.new_nonce())
        assert len(greeting) == 1 + 32
        assert greeting[0] == na.AUTH_VERSION

    def test_server_parse_splits_response(self):
        nonce = na.new_nonce()
        resp = na.response_for(b"tok", nonce)
        parsed = na.parse_greeting(bytes([na.AUTH_VERSION]) + resp)
        assert parsed == resp

    def test_parse_greeting_rejects_wrong_tag(self):
        with pytest.raises(na.AuthError):
            na.parse_greeting(bytes([0xFF]) + b"\x00" * 32)

    def test_parse_greeting_rejects_short(self):
        with pytest.raises(na.AuthError):
            na.parse_greeting(bytes([na.AUTH_VERSION]) + b"\x00" * 10)


class TestHandshake:
    """Server-side handshake over real sockets (loopback, ephemeral port)."""

    def test_handshake_ok_returns_client_marker(self):
        srv, port = _listen()
        token = b"tok"
        results = {}

        def serve():
            c, _ = srv.accept()
            results["id"] = na.server_handshake(c, {token: "ci-runner"})
            c.close()

        t = threading.Thread(target=serve, daemon=True)
        t.start()
        c = socket.create_connection(("127.0.0.1", port), timeout=3)
        nonce = c.recv(32)
        assert len(nonce) == 32
        c.sendall(na.client_greeting(token, nonce))
        t.join(timeout=3)
        c.close()
        srv.close()
        assert results.get("id") == "ci-runner"

    def test_handshake_wrong_token_disconnects(self):
        srv, port = _listen()

        def serve():
            c, _ = srv.accept()
            try:
                na.server_handshake(c, {b"real": "agent-a"})
            except na.AuthError:
                results["rejected"] = True
            c.close()

        results = {}
        t = threading.Thread(target=serve, daemon=True)
        t.start()
        c = socket.create_connection(("127.0.0.1", port), timeout=3)
        nonce = c.recv(32)
        c.sendall(na.client_greeting(b"evil", nonce))
        t.join(timeout=3)
        # Server must have closed without serving any framed data
        c.settimeout(2)
        assert c.recv(64) == b"" or results.get("rejected")
        c.close()
        srv.close()

    def test_handshake_timeout_enforced(self):
        """A silent client is dropped at the auth window, not held forever."""
        srv, port = _listen()
        results = {}

        def serve():
            c, _ = srv.accept()
            t0 = time.monotonic()
            try:
                na.server_handshake(c, {b"real": "agent-a"})
                results["accepted"] = True
            except na.AuthError:
                results["elapsed"] = time.monotonic() - t0
            c.close()

        t = threading.Thread(target=serve, daemon=True)
        t.start()
        c = socket.create_connection(("127.0.0.1", port), timeout=3)
        c.recv(32)  # take the nonce, then go silent
        t.join(timeout=5)
        c.close()
        srv.close()
        assert "elapsed" in results
        assert 1.0 <= results["elapsed"] <= 4.0

    def test_handshake_replay_rejected(self):
        srv, port = _listen()
        token = b"tok"
        results = []

        def serve():
            for _ in range(2):
                c, _ = srv.accept()
                try:
                    na.server_handshake(c, {token: "ci-runner"})
                    results.append("ok")
                except na.AuthError:
                    results.append("rejected")
                c.close()

        t = threading.Thread(target=serve, daemon=True)
        t.start()
        # Capture a valid (nonce, response) pair and complete handshake 1
        c1 = socket.create_connection(("127.0.0.1", port), timeout=3)
        with c1:
            nonce = c1.recv(32)
            resp = na.response_for(token, nonce)
            c1.sendall(na.client_greeting(token, nonce))
        # Handshake completes (nonce claimed)
        c2 = socket.create_connection(("127.0.0.1", port), timeout=3)
        with c2:
            _ = c2.recv(32)  # fresh nonce; ignore
            c2.sendall(bytes([na.AUTH_VERSION]) + resp)  # replay old pair
        t.join(timeout=3)
        srv.close()
        assert results == ["ok", "rejected"]

    def test_multi_token_table(self):
        """Server holds {token: token-id} for the whole team."""
        srv, port = _listen()
        tokens = {b"tok-a": "agent-a", b"tok-b": "agent-b"}
        got = {}

        def serve():
            c, _ = srv.accept()
            try:
                got["id"] = na.server_handshake(c, tokens)
            except na.AuthError:
                pass
            c.close()

        t = threading.Thread(target=serve, daemon=True)
        t.start()
        c = socket.create_connection(("127.0.0.1", port), timeout=3)
        nonce = c.recv(32)
        c.sendall(na.client_greeting(b"tok-b", nonce))
        t.join(timeout=3)
        c.close()
        srv.close()
        assert got["id"] == "agent-b"


class TestClientSide:
    def test_perform_handshake_roundtrip(self):
        """The shipped client helper authenticates against the shipped
        server helper on a live socket."""
        srv, port = _listen()
        token = b"tok"

        def serve():
            c, _ = srv.accept()
            try:
                na.server_handshake(c, {token: "ci"})
                got["ok"] = True
            except na.AuthError:
                pass
            c.close()

        got = {}
        t = threading.Thread(target=serve, daemon=True)
        t.start()
        c = socket.create_connection(("127.0.0.1", port), timeout=3)
        na.client_handshake(c, token)
        c.close()
        t.join(timeout=3)
        srv.close()
        assert got.get("ok") is True

    def test_client_handshake_wrong_token_raises(self):
        """A wrong token raises AuthError INSIDE client_handshake: the
        server closes instead of sending AUTH_ACCEPT, so the client fails
        before any request is written and can report
        network_auth_rejected (distinct from a dead daemon)."""
        srv, port = _listen()

        def serve():
            c, _ = srv.accept()
            try:
                na.server_handshake(c, {b"real": "a"})
                got["ok"] = True  # would be a bug
            except na.AuthError:
                pass
            c.close()

        got = {}
        t = threading.Thread(target=serve, daemon=True)
        t.start()
        c = socket.create_connection(("127.0.0.1", port), timeout=3)
        with pytest.raises(na.AuthError):
            na.client_handshake(c, b"wrong")
        c.close()
        t.join(timeout=3)
        srv.close()
        assert "ok" not in got

    def test_client_handshake_accept_frame_received(self):
        """The happy path consumes the AUTH_ACCEPT byte — request framing
        starts clean, no stray byte in the stream."""
        srv, port = _listen()

        def serve():
            c, _ = srv.accept()
            try:
                na.server_handshake(c, {b"real": "a"})
                from toolrecall.transport import receive_message, send_message

                req = receive_message(c)
                send_message(c, {"echo": req.get("cmd")})
            except Exception:
                pass
            c.close()

        t = threading.Thread(target=serve, daemon=True)
        t.start()
        c = socket.create_connection(("127.0.0.1", port), timeout=3)
        na.client_handshake(c, b"real")
        from toolrecall.transport import send_message, receive_message

        send_message(c, {"cmd": "ping"})
        resp = receive_message(c)
        c.close()
        t.join(timeout=3)
        srv.close()
        assert resp == {"echo": "ping", "v": 1}
