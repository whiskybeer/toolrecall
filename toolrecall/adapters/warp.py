# Copyright (c) 2026 Robin Schultka
# SPDX-License-Identifier: MIT
# Source: https://github.com/whiskybeer/toolrecall
"""Warp adapter — route Warp's custom-inference-endpoint traffic through
ToolRecall's forward proxy.

Why an edge is needed
---------------------
Warp's custom inference endpoint (https://docs.warp.dev/agents/inference/
custom-inference-endpoint/) makes Warp's *backend* call your endpoint, so it
must be reachable at a **public HTTPS URL** (localhost is rejected). The
ToolRecall forward proxy is a localhost service (port 8569) that resolves the
real LLM API host from the ``X-Target-Host`` header / ``Host`` header / path
prefix. Warp will send ``Host: <your-public-domain>`` and knows nothing about
``X-Target-Host``.

The Warp edge closes that gap:

    Warp backend --> public HTTPS edge (this module) --> TR proxy (8569) --> LLM API

The edge is deliberately tiny (stdlib only):

1.  Accept the request from Warp (``Host: <public-domain>``).
2.  Annotate it with the real provider host (``X-Target-Host: api.openai.com``)
    derived from the provider binding configured at setup time.
3.  Forward to the ToolRecall proxy, which applies its existing SSRF
    allowlist, request-hash cache, and replay logic.
4.  Relay the response (status, headers, body) back to Warp.

The provider binding (which LLM API sits behind the edge) is fixed at setup
time — one edge per provider. This keeps the SSRF surface at zero: the edge
adds exactly one header and never interprets caller input as a host.

Security notes
--------------
- The edge binds to 127.0.0.1 by default and is expected to be published via
  an HTTPS tunnel (cloudflared, ngrok, Caddy, etc.). TLS termination happens
  at the tunnel, not here.
- Warp sends the user's provider API key through its backend in-flight; the
  edge never logs or stores Authorization headers.
- The TR proxy's FORWARD_HOSTS allowlist still applies downstream — the edge
  cannot be used to reach non-LLM hosts.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import http.client
import http.server
import json
import os
import select
import socketserver
import threading
import time

TOOLRECALL_PROXY_HOST = os.environ.get("TOOLRECALL_PROXY_HOST", "127.0.0.1")
TOOLRECALL_PROXY_PORT = int(os.environ.get("TOOLRECALL_PROXY_PORT", "8569"))

# Seconds of upstream SSE silence before the edge injects a `: keepalive`
# comment frame. Must be well below intermediate-hop byte-gap timeouts
# (Warp's GCP relay ~30s; nginx here 300s). Override for tests.
_SSE_KEEPALIVE_INTERVAL = float(os.environ.get("TOOLRECALL_SSE_KEEPALIVE", "15"))

# Providers Warp users most commonly bind. Values are X-Target-Host targets
# the TR proxy already allowlists.
KNOWN_PROVIDERS = (
    "api.openai.com",
    "api.anthropic.com",
    "generativelanguage.googleapis.com",
    "api.deepseek.com",
    "api.x.ai",
    "api.mistral.ai",
    "api.groq.com",
    "api.together.xyz",
    "openrouter.ai",
)

# Headers not copied upstream verbatim.
_CAP_SEQ = [0]
_CAP_LOCK = threading.Lock()

_HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "upgrade",
    "transfer-encoding",
    "content-length",  # re-set explicitly
    "host",  # replaced with the TR proxy host
    "accept-encoding",  # identity requested so the relay is byte-safe
}


def make_request_hash(method: str, host: str, path: str, body: bytes) -> str:
    """Deterministic request hash — mirrors the proxy's key derivation.

    Provided so Warp-side tooling (eval harness, billing attribution) can
    predict which requests will cache-hit without making a network call.
    """
    body_hash = hashlib.sha256(body).hexdigest()
    request_str = f"{method}:{host}:{path}:{body_hash}"
    return hashlib.sha256(request_str.encode()).hexdigest()


def normalize_provider(provider: str) -> str:
    """Normalize a provider argument to a bare allowlisted hostname."""
    p = provider.strip().lower()
    p = p.removeprefix("https://").removeprefix("http://")
    p = p.split("/", 1)[0]
    if p not in KNOWN_PROVIDERS:
        raise ValueError(
            f"provider {provider!r} is not in the TR forward allowlist; "
            f"choose one of: {', '.join(KNOWN_PROVIDERS)}"
        )
    return p


def build_edge_config(
    provider: str,
    public_url: str,
    proxy_host: str = TOOLRECALL_PROXY_HOST,
    proxy_port: int = TOOLRECALL_PROXY_PORT,
) -> dict:
    """Build the edge configuration record (persisted by the CLI subcommand).

    Warp requirement: the endpoint URL must be public HTTPS — localhost and
    private addresses are rejected by Warp when configuring the endpoint.
    """
    norm = normalize_provider(provider)
    if not public_url.startswith("https://"):
        raise ValueError(
            "public_url must be an https:// URL — Warp rejects localhost and "
            "private addresses for custom inference endpoints"
        )
    return {
        "provider": norm,
        "public_url": public_url,
        "proxy_host": proxy_host,
        "proxy_port": proxy_port,
        "version": 1,
    }


class WarpEdgeHandler(http.server.BaseHTTPRequestHandler):
    # class default; make_edge_server overrides from TOOLRECALL_EDGE_CANONICALIZE
    canonicalize_profile: str | None = None

    """Translate Warp's request into an X-Target-Host-annotated call to the
    ToolRecall forward proxy.

    Class attributes (set by run_edge) hold the provider binding so the
    handler stays stateless per request.
    """

    provider_host: str = "api.openai.com"
    proxy_host: str = TOOLRECALL_PROXY_HOST
    proxy_port: int = TOOLRECALL_PROXY_PORT
    # Shared-secret ingress auth. When set, requests missing a valid
    # `Authorization: Bearer <token>` header are rejected 401 before any relay.
    # Required whenever the edge is exposed beyond loopback (tunnel, LAN).
    auth_token: str | None = None

    def log_message(self, fmt: str, *args) -> None:  # noqa: D102
        # Never log paths or headers: Warp requests carry the user's API key
        # in-flight. Suppress default access logging entirely.
        return

    # -- helpers ---------------------------------------------------------

    def _authorized(self) -> bool:
        token = type(self).auth_token
        if not token:
            return True
        auth = self.headers.get("Authorization", "").strip()
        # compare_digest: no timing side-channel on the secret. Tokens are
        # compared whitespace-tolerantly: paste artifacts (trailing space/newline
        # from copy&paste into Warp's settings) must not fail auth — the real
        # -world failure mode observed in the Warp live-test.
        auth_n = " ".join(auth.split())  # collapse runs of whitespace
        expect = " ".join(f"Bearer {token}".split())
        return hmac.compare_digest(auth_n, expect)

    def _relay(self, method: str) -> None:
        if not self._authorized():
            self.send_response(401)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""

        # Capture mode (TOOLRECALL_EDGE_CAPTURE_DIR): dump method, path, body
        # hash and size per request. For the Warp live-test — determining
        # whether Warp's server-side harness sends byte-identical bodies for
        # semantically identical requests. Bodies contain prompts; the dump
        # dir must never be web-served.
        cap_dir = os.environ.get("TOOLRECALL_EDGE_CAPTURE_DIR", "")
        if cap_dir and body:
            cap = {
                "seq": _CAP_SEQ[0],
                "method": method,
                "path": self.path,
                "sha256": hashlib.sha256(body).hexdigest(),
                "len": len(body),
            }
            with _CAP_LOCK:
                _CAP_SEQ[0] += 1
            try:
                base = os.environ["TOOLRECALL_EDGE_CAPTURE_DIR"]
                os.makedirs(base, exist_ok=True)
                with open(os.path.join(base, "index.jsonl"), "a") as f:
                    f.write(json.dumps(cap) + "\n")
                with open(os.path.join(base, f"body_{_CAP_SEQ[0]:04d}.json"), "wb") as f:
                    f.write(body)
            except OSError:
                pass  # capture is best-effort, never blocks relay

        conn = http.client.HTTPConnection(self.proxy_host, self.proxy_port, timeout=300)
        try:
            headers = {k: v for k, v in self.headers.items() if k.lower() not in _HOP_BY_HOP}
            # The one annotation the TR proxy needs: which provider is behind us.
            headers["X-Target-Host"] = self.provider_host
            # Auto-canonicalization: this edge fronts Warp's harness, whose
            # bodies embed per-session volatile fields (run UUID, timestamps,
            # env state — measured live 2026-09-03). Without canonicalization
            # the api_cache hit rate behind Warp is 0%. The profile is a
            # property of the edge deployment, not the client request.
            if self.canonicalize_profile:
                headers["X-ToolRecall-Canonicalize"] = self.canonicalize_profile
            headers["Host"] = f"{self.proxy_host}:{self.proxy_port}"
            headers["Accept-Encoding"] = "identity"
            headers["Content-Length"] = str(len(body))
            conn.request(method, self.path, body=body, headers=headers)
            resp = conn.getresponse()

            # True streaming relay: forward chunks as they arrive instead of
            # buffering the whole upstream response. Warp's client (reqwest)
            # starts its read timeout on connect — for long agent-turn
            # generations (~minutes on large contexts) a buffered relay
            # delivers zero bytes until completion, and the client times out
            # mid-wait ("peer closed connection without sending TLS
            # close_notify" — measured live 2026-09-04, 88K-token turns).
            is_sse = "text/event-stream" in (resp.getheader("Content-Type") or "")
            # Declare our own framing BEFORE writing anything: upstream
            # Content-Length is stripped (hop-by-hop), so we must know the
            # real body size. Reading the body first also means a fabricated
            # length can never truncate the response at the client (live
            # 2026-09-05: upstream without Content-Length yielded
            # `Content-Length: 0` and the client saw b'').
            self.send_response(resp.status)
            for k, v in resp.getheaders():
                if k.lower() in _HOP_BY_HOP:
                    continue
                self.send_header(k, v)
            if is_sse:
                # SSE has no declared length — terminate by closing the
                # connection after the stream ends. Bytes still flow to the
                # client the moment the upstream flushes them.
                self.send_header("Connection", "close")
                self.close_connection = True
                self.end_headers()
                # SSE keepalive: inject `: keepalive` comment frames while the
                # upstream is silent. Warp's requests reach us through a
                # backend relay (GCP LB, measured 2026-09-06) that kills
                # connections with no bytes for ~30s — exactly the signature of
                # a long reasoning/tool-call generation phase (46K-token prompts
                # stream nothing for the first tens of seconds). SSE clients
                # ignore comment frames, so these are invisible to the model
                # output; they only keep every intermediate hop alive.
                # Keepalive via select(): wait up to _SSE_KEEPALIVE_INTERVAL
                # for upstream readability. Timeout = silence → emit a
                # `: keepalive` comment frame and re-arm. (A socket timeout
                # on resp.read() is NOT usable: http.client's response object
                # dies on the first timeout — measured, OSError 'cannot read
                # from timed out object'. select leaves it healthy.)
                last_data = time.monotonic()
                # Drill down to the live socket for select(). mypy can't see
                # through http.client's wrapped stream stack — the runtime
                # attribute exists (verified live), so the typed guard below
                # satisfies both.
                raw = getattr(resp.fp, "raw", None)
                upstream_sock = getattr(raw, "_sock", None)
                assert upstream_sock is not None, "SSE upstream has no socket"
                while True:
                    readable, _, _ = select.select([upstream_sock], [], [], _SSE_KEEPALIVE_INTERVAL)
                    if not readable:
                        if time.monotonic() - last_data >= _SSE_KEEPALIVE_INTERVAL:
                            self.wfile.write(b": keepalive\n\n")
                            self.wfile.flush()
                            last_data = time.monotonic()
                        continue
                    buf = resp.read(4096)
                    if not buf:
                        break
                    self.wfile.write(buf)
                    self.wfile.flush()
                    last_data = time.monotonic()
            else:
                body_out = resp.read()
                self.send_header("Content-Length", str(len(body_out)))
                self.end_headers()
                self.wfile.write(body_out)
        except (BrokenPipeError, ConnectionResetError):
            # Client hung up mid-stream (Warp abort/retry) — nothing to do;
            # the upstream request completes harmlessly.
            pass
        except (OSError, http.client.HTTPException) as exc:
            try:
                self.send_response(502)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", "0")
                self.end_headers()
            except OSError:
                pass  # client already gone
            # Error detail goes to stderr only — never into the response.
            print(f"[tr-warp-edge] upstream error: {exc!r}", file=__import__("sys").stderr)
        finally:
            conn.close()

    def do_GET(self) -> None:  # noqa: N802
        self._relay("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._relay("POST")

    def do_PUT(self) -> None:  # noqa: N802
        self._relay("PUT")

    def do_DELETE(self) -> None:  # noqa: N802
        self._relay("DELETE")


class ThreadedEdgeServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def make_edge_server(
    provider: str,
    bind: str = "127.0.0.1",
    port: int = 0,
    proxy_host: str = TOOLRECALL_PROXY_HOST,
    proxy_port: int = TOOLRECALL_PROXY_PORT,
    auth_token: str | None = None,
) -> ThreadedEdgeServer:
    """Build (not start) an edge server. port=0 picks a free ephemeral port.

    `run_edge` is the blocking CLI wrapper around this factory; tests and
    embeddings can call serve_forever() themselves.

    auth_token: shared secret for ingress auth. Strongly recommended whenever
    the edge is published beyond loopback (tunnel, LAN) — a quick tunnel URL
    is public-by-obscurity, not private. Falls back to TOOLRECALL_EDGE_TOKEN.
    """
    norm = normalize_provider(provider)
    handler: type[WarpEdgeHandler] = type("BoundWarpEdgeHandler", (WarpEdgeHandler,), {})
    handler.provider_host = norm
    handler.proxy_host = proxy_host
    handler.proxy_port = proxy_port
    handler.auth_token = auth_token or os.environ.get("TOOLRECALL_EDGE_TOKEN") or None
    profile = (os.environ.get("TOOLRECALL_EDGE_CANONICALIZE") or "").strip().lower()
    # 'auto' = this edge fronts Warp's harness by design → use the warp profile
    handler.canonicalize_profile = {"auto": "warp"}.get(profile, profile) or None
    return ThreadedEdgeServer((bind, port), handler)


def run_edge(
    provider: str,
    bind: str = "127.0.0.1",
    port: int = 8571,
    proxy_host: str = TOOLRECALL_PROXY_HOST,
    proxy_port: int = TOOLRECALL_PROXY_PORT,
    auth_token: str | None = None,
) -> None:
    """Start the Warp edge. Publish `bind:port` via an HTTPS tunnel for Warp."""
    server = make_edge_server(
        provider,
        bind=bind,
        port=port,
        proxy_host=proxy_host,
        proxy_port=proxy_port,
        auth_token=auth_token,
    )
    norm = normalize_provider(provider)
    print(
        f"[tr-warp-edge] provider={norm} edge=http://{bind}:{port} proxy={proxy_host}:{proxy_port}",
        flush=True,
    )
    handler_cls = server.RequestHandlerClass
    assert isinstance(handler_cls, type) and issubclass(handler_cls, WarpEdgeHandler)
    if handler_cls.auth_token:
        print(
            "[tr-warp-edge] auth: enabled (TOOLRECALL_EDGE_TOKEN / --auth-token). "
            "Requests must carry 'Authorization: Bearer <token>'.",
            flush=True,
        )
    else:
        print(
            "[tr-warp-edge] WARNING: auth disabled. Fine on loopback only — if you "
            "publish this edge via a tunnel, set TOOLRECALL_EDGE_TOKEN: a quick-tunnel "
            "URL is public-by-obscurity, not private.",
            flush=True,
        )
    print(
        "[tr-warp-edge] publish this edge at a public HTTPS URL (cloudflared, "
        "ngrok, Caddy, ...) and register that URL in Warp: Settings > "
        "inference endpoint.",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="tr-warp-edge",
        description="Expose the ToolRecall forward proxy as a Warp custom inference endpoint.",
    )
    parser.add_argument(
        "--provider",
        required=True,
        help=f"LLM API host behind the edge. One of: {', '.join(KNOWN_PROVIDERS)}",
    )
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8571)
    parser.add_argument("--proxy-host", default=TOOLRECALL_PROXY_HOST)
    parser.add_argument("--proxy-port", type=int, default=TOOLRECALL_PROXY_PORT)
    parser.add_argument(
        "--auth-token",
        default=None,
        help="Shared secret for ingress auth (falls back to TOOLRECALL_EDGE_TOKEN). "
        "Set this when publishing the edge via a public tunnel.",
    )
    args = parser.parse_args(argv)

    try:
        run_edge(
            provider=args.provider,
            bind=args.bind,
            port=args.port,
            proxy_host=args.proxy_host,
            proxy_port=args.proxy_port,
            auth_token=args.auth_token,
        )
    except ValueError as exc:
        print(f"error: {exc}", file=__import__("sys").stderr)
        return 2
    except OSError as exc:
        print(f"error: cannot bind edge: {exc}", file=__import__("sys").stderr)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
