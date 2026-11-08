"""Live E2E: ETag revalidation against a real local HTTP server.

Boots a stdlib http.server with ETag support on localhost (SSRF validator
explicitly allows loopback for these tests via its allowlist... no — the
validator BLOCKS loopback by design). Therefore these tests drive
_fetch_revalidate_once's REQUEST path through a monkeypatched _validate_url
(read-only stub: returns None = valid) while everything else — real sockets,
real HTTP, real 304/200 semantics — runs live. The ETag contract itself is
tested end-to-end; the SSRF validator is separately unit-tested in
test_windows_bugs/test_security suites.

Runs marked e2e (make validate excludes them; make test-e2e includes).
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

import toolrecall.client as tr_client
from toolrecall import cache as tr_cache

ETAG_V1 = '"ver1"'
ETAG_V2 = '"ver2"'
BODY_V1 = "<html>version-one</html>"
BODY_V2 = "<html>version-two</html>"


class _EtagHandler(BaseHTTPRequestHandler):
    """Tiny origin: /doc serves BODY_V1/V2; /changed switches after
    flip[0] = True. ETag = version marker; honors If-None-Match."""

    flip = [False]

    def do_GET(self):
        if self.path.startswith("/doc"):
            etag = ETAG_V2 if self.flip[0] else ETAG_V1
            body = (BODY_V2 if self.flip[0] else BODY_V1).encode()
            if self.headers.get("If-None-Match") == etag:
                self.send_response(304)
                self.send_header("ETag", etag)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("ETag", etag)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *a):  # silence request logging
        pass


@pytest.fixture(scope="module")
def origin():
    srv = HTTPServer(("127.0.0.1", 0), _EtagHandler)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{port}"
    srv.shutdown()


@pytest.fixture(autouse=True)
def _reset_db_singleton(_isolated_toolrecall_env):
    import toolrecall._db as dbmod

    dbmod._cached_config = None
    if getattr(dbmod, "_db_real", None) is not None:
        try:
            dbmod._db_real.close()
        except Exception:
            pass
        dbmod._db_real = None
    from toolrecall.cache import SCHEMA

    dbmod._init(SCHEMA)
    yield


@pytest.fixture()
def daemon_up(monkeypatch):
    monkeypatch.setattr(tr_client, "daemon_running", lambda: True)


@pytest.fixture()
def loopback_allowed(monkeypatch):
    """The SSRF validator blocks loopback by design; these tests point it at
    the local origin. Stub is read-only passthrough (never widens scope)."""
    import toolrecall.daemon as daemon_mod

    monkeypatch.setattr(daemon_mod, "_validate_url_allow_all", True, raising=False)
    # _fetch_revalidate_once loads mcp_fetch dynamically; patch its validator
    import importlib.util

    spec = importlib.util.find_spec("toolrecall.mcp_fetch")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "_validate_url", lambda url: None)

    def patched(url, etag):
        return _direct_revalidate(mod, url, etag)

    return patched


def _direct_revalidate(mcp_fetch, url: str, etag: str | None):
    """Same request logic as daemon._fetch_revalidate_once, with the module
    (and thus the patched validator) passed in explicitly.

    etag=None → plain GET (the initial capture fetch)."""
    from urllib.error import HTTPError
    from urllib.request import Request, build_opener

    req = Request(url, method="GET")
    req.add_header("User-Agent", "toolrecall-fetch-mcp/0.1")
    if etag:
        req.add_header("If-None-Match", etag)
    opener = build_opener(mcp_fetch._no_redirects())
    try:
        with opener.open(req, timeout=10) as resp:
            headers = dict(resp.headers)
            raw = resp.read(mcp_fetch.MAX_CONTENT_BYTES)
            return {
                "status": resp.status,
                "content": raw.decode("utf-8", errors="replace"),
                "content_type": headers.get("Content-Type", ""),
                "etag": headers.get("ETag"),
            }
    except HTTPError as e:
        if e.code == 304:
            return {"status": 304, "revalidated": True, "etag": etag}
        return {"status": e.code, "error": str(e.reason)}


def _expire_all():
    import time

    with tr_cache._db() as conn:
        conn.execute("UPDATE mcp_cache SET expires_at = ?", (time.time() - 1,))


class TestLiveRevalidation:
    def test_full_lifecycle_304(self, origin, daemon_up, loopback_allowed):
        url = f"{origin}/doc"
        args = {"url": url}
        # 1. live fetch → store with etag (as the transparent cache would)
        body = _direct_revalidate(_load_mcp_fetch(), url, None)
        assert body["status"] == 200 and body["content"] == BODY_V1
        h = tr_cache._mcp_request_key("fetch", "fetch_url", args)
        tr_cache.cached_mcp_store(
            h, "fetch", "fetch_url", args, body["content"], ttl=60, etag=body["etag"]
        )
        # 2. expire the row (simulate TTL passing)
        _expire_all()
        # 3. revalidate → 304 → row refreshed, body kept
        outcome = tr_cache.revalidate_mcp_entry(
            "fetch", "fetch_url", args, fetch_fn=lambda etag: loopback_allowed(url, etag)
        )
        assert outcome["action"] == "revalidated"
        with tr_cache._db() as conn:
            row = conn.execute(
                "SELECT data, etag FROM mcp_cache WHERE request_hash = ?", (h,)
            ).fetchone()
        assert row["data"] == BODY_V1  # old body kept — no transfer
        assert row["etag"] == ETAG_V1

    def test_changed_content_200_replaces(self, origin, daemon_up, loopback_allowed):
        url = f"{origin}/doc"
        args = {"url": url}
        h = tr_cache._mcp_request_key("fetch", "fetch_url", args)
        tr_cache.cached_mcp_store(h, "fetch", "fetch_url", args, BODY_V1, ttl=60, etag=ETAG_V1)
        _expire_all()
        _EtagHandler.flip[0] = True  # origin now serves version two
        try:
            outcome = tr_cache.revalidate_mcp_entry(
                "fetch", "fetch_url", args, fetch_fn=lambda etag: loopback_allowed(url, etag)
            )
            assert outcome["action"] == "replaced"
            with tr_cache._db() as conn:
                row = conn.execute(
                    "SELECT data, etag FROM mcp_cache WHERE request_hash = ?", (h,)
                ).fetchone()
            assert json.loads(row["data"])["content"] == BODY_V2
            assert row["etag"] == ETAG_V2
        finally:
            _EtagHandler.flip[0] = False


def _load_mcp_fetch():
    import importlib.util

    spec = importlib.util.find_spec("toolrecall.mcp_fetch")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod
