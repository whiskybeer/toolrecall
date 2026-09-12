"""network_auth — HMAC-SHA256 challenge-response for the shared network daemon.

A6 (constitution X): the daemon is localhost-UDS only by default. When
``[network]`` is enabled it additionally listens on TCP so a whole team can
warm one cache. Every TCP connection must first authenticate:

    server → client : 32-byte random nonce
    client → server : 0x01 tag + HMAC_SHA256(token, nonce)   (32 bytes)
    server          : verifies in constant time, claims the nonce in a
                      replay cache, then either serves or disconnects.

Design properties:

- **The token never crosses the wire.** Only a per-connection HMAC of a
  fresh nonce does; capturing traffic reveals nothing reusable.
- **Replay-proof per connection.** Each nonce is claimed exactly once
  (LRU-bounded cache). A replayed (nonce, response) pair fails.
- **Constant-time comparison** via ``hmac.compare_digest``.
- **Bounded auth window.** A client that doesn't complete the handshake
  within AUTH_WINDOW_S seconds is dropped — no slow-loris socket held.
- **Zero dependencies** (constitution II): only stdlib hmac/secrets/threading.

TLS (2.2b): the listener may wrap accepted sockets with the stdlib ``ssl``
module before this handshake — see toolrecall/network_tls.py. The HMAC
handshake runs identically on top of TLS; the two layers are independent
(authenticates the client, TLS encrypts the channel).

Version: AUTH_VERSION = 1. A future breaking change bumps it; the server
rejects unknown tags with a clean disconnect.
"""

from __future__ import annotations

import hmac
import secrets
import socket
import ssl
import threading
from collections import OrderedDict

# ─── Protocol constants ───────────────────────────────────

#: Handshake version tag. Prefixes the client's response so the server can
#: distinguish handshake bytes from out-of-framing garbage at zero cost.
AUTH_VERSION = 1

#: Nonce length in bytes (sha256 output size).
NONCE_SIZE = 32

#: Seconds a client has to complete the auth handshake before disconnect.
#: Short on purpose: an unauthenticated socket is attacker-controlled
#: surface; nothing legitimate takes >3s to answer a nonce.
AUTH_WINDOW_S = 3.0


class AuthError(Exception):
    """Raised server-side when a handshake fails (wrong token, replay,
    timeout, malformed greeting) and client-side when the server rejects
    us or drops the connection mid-handshake."""


#: One-byte server → client accept frame after a verified proof. Lets the
#: client report "auth rejected" distinctly from "daemon died" (honesty).
AUTH_ACCEPT = b"\x01"


# ─── Primitives (pure, unit-testable) ─────────────────────


def new_nonce() -> bytes:
    """Cryptographically random 32-byte nonce (server → client)."""
    return secrets.token_bytes(NONCE_SIZE)


def response_for(token: bytes | str, nonce: bytes) -> bytes:
    """Client proof: HMAC_SHA256(token, nonce). Token coerced from str."""
    if isinstance(token, str):
        token = token.encode("utf-8")
    return hmac.new(token, nonce, "sha256").digest()


def verify_response(token: bytes | str, nonce: bytes, response: bytes, cache: "NonceCache") -> bool:
    """Verify a client's proof. Constant-time compare; claims the nonce so
    the same (nonce, response) pair can never verify twice."""
    if isinstance(token, str):
        token = token.encode("utf-8")
    if not isinstance(response, (bytes, bytearray)) or len(response) != NONCE_SIZE:
        return False
    expected = hmac.new(token, nonce, "sha256").digest()
    if not hmac.compare_digest(expected, bytes(response)):
        return False
    # Only burn the nonce on a VALID proof: a wrong-guess flood must not
    # be able to exhaust the replay cache by claiming nonces it can't
    # authenticate against.
    return cache.claim(nonce)


class NonceCache:
    """Thread-safe LRU of spent nonces (replay protection).

    Bounds: memory is capped at ``maxsize`` entries. A nonce evicted before
    its replay window closes is the accepted trade-off — exploiting it
    requires forcing maxsize+1 successful handshakes within one auth window
    (3s) while reusing the captured pair; the pair also stays valid only
    against the *same* token, and TLS deployments are immune (channel bound).

    Note only VALID proofs claim nonces (see verify_response), so an
    unauthenticated flood cannot rotate the cache.
    """

    def __init__(self, maxsize: int = 4096):
        self._maxsize = maxsize
        self._seen: OrderedDict[bytes, None] = OrderedDict()
        self._lock = threading.Lock()

    def claim(self, nonce: bytes) -> bool:
        """Mark nonce as spent. True the first time, False on any repeat."""
        with self._lock:
            if nonce in self._seen:
                self._seen.move_to_end(nonce)
                return False
            self._seen[nonce] = None
            while len(self._seen) > self._maxsize:
                self._seen.popitem(last=False)
            return True

    def seen(self, nonce: bytes) -> bool:
        with self._lock:
            return nonce in self._seen


# ─── Wire helpers ─────────────────────────────────────────


def client_greeting(token: bytes | str, nonce: bytes) -> bytes:
    """Build the client's greeting: version tag + HMAC response."""
    return bytes([AUTH_VERSION]) + response_for(token, nonce)


def parse_greeting(data: bytes) -> bytes:
    """Server-side parse of the greeting. Returns the 32-byte response.

    Raises AuthError on wrong version tag or short payload.
    """
    if len(data) < 1 + NONCE_SIZE:
        raise AuthError(f"greeting too short ({len(data)} bytes)")
    if data[0] != AUTH_VERSION:
        raise AuthError(f"unsupported auth version {data[0]}")
    return data[1 : 1 + NONCE_SIZE]


def _recv_exact(conn: socket.socket, n: int) -> bytes:
    """Read exactly n bytes; AuthError on clean EOF / reset mid-read.

    (transport.recv_exact returns short reads on EOF — here a short read
    during the handshake IS the failure mode, so we convert it.)
    """
    chunks = bytearray()
    while len(chunks) < n:
        try:
            chunk = conn.recv(n - len(chunks))
        except OSError as e:
            raise AuthError(f"socket error during handshake: {e}") from e
        if not chunk:
            raise AuthError("peer closed during handshake")
        chunks.extend(chunk)
    return bytes(chunks)


# ─── Handshake (socket-level) ─────────────────────────────


def server_handshake(conn: socket.socket, tokens: dict) -> str:
    """Authenticate one inbound connection. Returns the token-id.

    Args:
        conn: accepted socket (server already sent nothing yet).
        tokens: {token-bytes: token-id} — the team's pre-shared tokens.
            A single-token caller passes {token: "local"}.

    Raises AuthError on any failure; caller disconnects and moves on.
    The 3-second window applies to the WHOLE handshake.
    """
    conn.settimeout(AUTH_WINDOW_S)
    nonce = new_nonce()
    try:
        conn.sendall(nonce)
        greeting = _recv_exact(conn, 1 + NONCE_SIZE)
    except (socket.timeout, TimeoutError) as e:
        raise AuthError("handshake timeout") from e
    except OSError as e:
        raise AuthError(f"socket error during handshake: {e}") from e

    response = parse_greeting(greeting)
    # Try every configured token in constant time per token; team tables
    # are small (handfuls). The nonce is claimed only on success.
    for token, token_id in tokens.items():
        if verify_response(token, nonce, response, _SHARED_CACHE):
            # Explicit accept frame: lets the client DISTINGUISH "auth
            # rejected" (EOF before ACK) from "daemon died mid-connection"
            # — error honesty, not convenience. One byte per connection.
            conn.sendall(AUTH_ACCEPT)
            return str(token_id)
    raise AuthError("auth failed: token mismatch or replayed nonce")


def client_handshake(
    conn: socket.socket, token: bytes | str, timeout: float = AUTH_WINDOW_S
) -> None:
    """Client side: read nonce, send proof, wait for the accept frame.

    The server sends a single AUTH_ACCEPT byte (0x01) after verifying the
    proof; anything else — clean EOF, reset, or a different byte — means
    rejection and raises AuthError BEFORE any request is written, so the
    caller can report ``network_auth_rejected`` instead of mistaking it
    for a dead daemon.

    Raises AuthError on rejection, timeout, or socket death.
    """
    if isinstance(token, str):
        token = token.encode("utf-8")
    # Peer discrimination (live-test finding, 2026-09-08): an A6 daemon
    # SPEAKS FIRST (32-byte nonce), a legacy framed peer (Windows loopback,
    # no auth) WAITS for our frame. Peek with a short window:
    #   - nonce bytes arrive fast  -> A6 daemon: run the HMAC greeting.
    #   - nothing within 0.25s     -> legacy peer waiting for our request:
    #     return without sending handshake bytes; plain framing proceeds.
    # (Both-wait deadlock otherwise: we peek, they recv, 3s timeout, error.)
    conn.settimeout(0.25)
    try:
        if not isinstance(conn, ssl.SSLSocket):
            try:
                peek = conn.recv(NONCE_SIZE, socket.MSG_PEEK)
            except ValueError:
                # Some socket flavors don't support MSG_PEEK — treat as A6.
                peek = b""
            if len(peek) >= 4 and len(peek) < NONCE_SIZE and peek[:1] != b"\x01":
                import struct as _s

                (declared,) = _s.unpack("!I", peek[:4])
                if declared <= 1024 * 1024:
                    # Framed-IPC length prefix, not a nonce: legacy peer.
                    conn.settimeout(None)
                    return
    except (socket.timeout, TimeoutError):
        # No first-speaker within 0.25s: a legacy framed peer waiting for
        # our request. Plain mode — the caller sends/receives frames
        # directly. (An A6 daemon ALWAYS sends its nonce immediately.)
        conn.settimeout(None)
        return
    try:
        conn.settimeout(AUTH_WINDOW_S)
        nonce = _recv_exact(conn, NONCE_SIZE)
        conn.sendall(client_greeting(token, nonce))
        verdict = _recv_exact(conn, 1)
        if verdict != AUTH_ACCEPT:
            raise AuthError("server rejected the handshake")
    except (socket.timeout, TimeoutError) as e:
        raise AuthError("handshake timeout (server did not send nonce)") from e
    except OSError as e:
        raise AuthError(f"socket error during handshake: {e}") from e


#: Process-wide replay cache. Shared across the daemon's connections so a
#: captured pair can't be replayed on a second connection either.
_SHARED_CACHE = NonceCache()
