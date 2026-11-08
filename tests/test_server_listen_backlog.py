"""Bursts of parallel connections must not wait on the listen backlog.

socketserver's default request_queue_size is 5; past that, Linux drops the
extra SYNs and clients retry after ~1 s. Parallel agents starting together
hit exactly that (bench/results/FLEET_RESULTS.md, Phase 0 check 4).
"""

import http.client
import http.server
import threading
import time

import pytest

from toolrecall.adapters.warp import ThreadedEdgeServer
from toolrecall.proxy import ThreadedHTTPServer

HANDLER_S = 0.5
BURST = 16


class _Slow(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        time.sleep(HANDLER_S)
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *a):
        pass


@pytest.mark.parametrize("server_cls", [ThreadedHTTPServer, ThreadedEdgeServer])
def test_backlog_is_large(server_cls):
    assert server_cls.request_queue_size >= 64


@pytest.mark.parametrize("server_cls", [ThreadedHTTPServer, ThreadedEdgeServer])
def test_burst_is_served_in_parallel(server_cls):
    srv = server_cls(("127.0.0.1", 0), _Slow)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    barrier = threading.Barrier(BURST)
    errors = []

    def hit():
        barrier.wait()
        try:
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            c.request("GET", "/")
            assert c.getresponse().read() == b"ok"
            c.close()
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    ts = [threading.Thread(target=hit) for _ in range(BURST)]
    t0 = time.time()
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    wall = time.time() - t0
    srv.shutdown()
    srv.server_close()
    assert not errors
    # one handler time plus slack; a SYN retry would add ~1 s
    assert wall < HANDLER_S + 0.4, wall
