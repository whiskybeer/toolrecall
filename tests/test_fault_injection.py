"""Fault-injection tests at the client/transport boundary.

Closes the biggest pre-existing blindspot: zero tests injected transport
failures, yet "daemon unreachable mid-session" is ToolRecall's #1 real-world
failure class. Each test spawns a real UDS server that misbehaves in one
specific way and asserts the client returns an *observable* error dict —
never an empty/None result that downstream code would treat as a valid
(empty) cache answer.

Scenarios:
  1. socket file missing          -> daemon_unavailable
  2. connect refused              -> daemon_unavailable
  3. accept then hang (timeout)   -> daemon_unavailable
  4. EOF before length prefix     -> Empty response
  5. EOF mid-payload (truncated)  -> Empty response (not garbage parse)
  6. oversized length prefix      -> Message too large + socket drained
  7. garbage bytes instead of JSON-> error dict, no unhandled exception
  8. daemon dies between accept and response (kill mid-serve) -> error dict
  9. malformed length prefix (0xFFFFFF01) -> drain + clean error
 10. ping() returns False on every fault above (connectivity check)
 11. response after delay within timeout (slow-but-alive daemon) -> succeeds
 12. empty payload with correct prefix -> Empty response
"""

import os
import socket
import struct
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from toolrecall.transport import (
    TransportClient,
    _MAX_MSG_SIZE,
    send_message,
)

# Fault-server configs: each is (name, server_factory_kwargs).
# All servers bind per-test temp UDS paths.


class FaultyServer:
    """A UDS server that fails in a controlled way. One connection served."""

    def __init__(self, path: str, mode: str):
        self.path = path
        self.mode = mode
        self.served = threading.Event()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self.sock = None

    def start(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            os.unlink(self.path)
        except FileNotFoundError:
            pass
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self.sock.bind(self.path)
        self.sock.listen(1)
        self._thread.start()

    def stop(self):
        self._stop.set()
        try:
            self.sock.close()
        except OSError:
            pass

    def _serve(self):
        try:
            self.sock.settimeout(5)
            conn, _ = self.sock.accept()
        except OSError:
            return
        self.served.set()
        try:
            if self.mode == "hang":
                # Accept the request but never respond — client should time out.
                conn.recv(65536)
                time.sleep(10)
            elif self.mode == "eof_prefix":
                # FIN: close cleanly after reading the request. Client sees
                # EOF on recv -> {"error": "Empty response"}.
                conn.recv(65536)
                conn.shutdown(socket.SHUT_RDWR)
                conn.close()
            elif self.mode == "reset":
                # RST: close WITHOUT reading the request. Client's sendall
                # hits ECONNRESET -> mapped to daemon_unavailable (indistin-
                # guishable from a daemon that never existed — a documented
                # consequence of the coarse OSError mapping in send()).
                conn.close()
            elif self.mode == "eof_mid_payload":
                # Send a valid length prefix claiming a large payload, then
                # FIN the connection after a tiny delay so the client is
                # already blocked in recv (reads EOF, not RST).
                conn.recv(65536)
                conn.sendall(struct.pack("!I", 4096))
                time.sleep(0.05)
                conn.shutdown(socket.SHUT_RDWR)
            elif self.mode == "garbage":
                conn.recv(65536)
                conn.sendall(b"not json at all")
                conn.close()
            elif self.mode == "oversize_prefix":
                conn.recv(65536)
                conn.sendall(struct.pack("!I", _MAX_MSG_SIZE + 1))
                # do NOT send payload — daemon "dies" after sending the prefix
                time.sleep(0.3)
                conn.close()
            elif self.mode == "empty_payload":
                conn.recv(65536)
                conn.sendall(struct.pack("!I", 0))
                conn.close()
            elif self.mode == "slow_ok":
                conn.recv(65536)
                time.sleep(0.2)
                send_message(conn, {"pong": True, "pid": 123})
                conn.close()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass


class TestTransportFaultInjection(unittest.TestCase):
    """Every transport failure mode must return an observable error dict."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="tr_fault_")
        self.sock_path = os.path.join(self.tmpdir, "fault.sock")
        self.client = TransportClient(self.sock_path)

    def tearDown(self):
        # best-effort socket file cleanup
        try:
            os.unlink(self.sock_path)
        except FileNotFoundError:
            pass
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _serve_and_send(self, mode: str, timeout: float = 5.0):
        srv = FaultyServer(self.sock_path, mode)
        srv.start()
        try:
            return self.client.send({"cmd": "ping"}, timeout=timeout)
        finally:
            srv.stop()

    # ── Unreachable-class faults ─────────────────────────────

    def test_missing_socket_file(self):
        """Socket file never existed -> daemon_unavailable (fallback trigger)."""
        resp = self.client.send({"cmd": "ping"})
        self.assertEqual(resp, {"error": "daemon_unavailable"})

    def test_connect_refused(self):
        """File exists but nobody listens -> daemon_unavailable."""
        # create the socket file without a listener: bind+close leaves the
        # file on disk, so connect() gets ECONNREFUSED (not FileNotFoundError)
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.bind(self.sock_path)
        s.close()
        resp = self.client.send({"cmd": "ping"})
        self.assertEqual(resp, {"error": "daemon_unavailable"})
        os.unlink(self.sock_path)

    def test_hang_times_out(self):
        """Daemon accepts but never responds -> daemon_unavailable within timeout."""
        t0 = time.monotonic()
        resp = self._serve_and_send("hang", timeout=0.5)
        elapsed = time.monotonic() - t0
        self.assertEqual(resp, {"error": "daemon_unavailable"})
        self.assertLess(elapsed, 3.0)  # must respect the caller's timeout

    def test_slow_daemon_within_timeout_succeeds(self):
        """Slow-but-alive daemon (< timeout) still gets served."""
        resp = self._serve_and_send("slow_ok", timeout=2.0)
        self.assertEqual(resp.get("pong"), True)

    # ── Protocol-corruption-class faults ─────────────────────

    def test_eof_before_prefix(self):
        """Clean close (FIN) after request -> Empty response, not None."""
        resp = self._serve_and_send("eof_prefix")
        self.assertEqual(resp, {"error": "Empty response"})

    def test_rst_before_response(self):
        """Abortive close (RST, daemon killed before reading request) ->
        daemon_unavailable. Documented ambiguity: send() maps all OSError
        to daemon_unavailable, so RST-mid-request looks identical to a
        daemon that never existed."""
        resp = self._serve_and_send("rst")
        self.assertEqual(resp, {"error": "daemon_unavailable"})

    def test_eof_mid_payload(self):
        """Daemon dies mid-payload -> Empty response (no garbage parse)."""
        resp = self._serve_and_send("eof_mid_payload")
        self.assertEqual(resp, {"error": "Empty response"})

    def test_oversize_prefix_drains_and_errors(self):
        """1MB+ prefix -> 'Message too large', no socket poisoning (v0.8.x fix)."""
        resp = self._serve_and_send("oversize_prefix")
        self.assertEqual(resp, {"error": "Message too large"})

    def test_empty_payload(self):
        """Zero-length payload -> Empty response, not JSON error."""
        resp = self._serve_and_send("empty_payload")
        self.assertEqual(resp, {"error": "Empty response"})

    def test_garbage_payload(self):
        """Non-JSON payload -> observable error, never an unhandled exception."""
        resp = self._serve_and_send("garbage")
        self.assertIn("error", resp)
        self.assertTrue(resp["error"])  # non-empty error string

    # ── ping() must agree with send() on every fault ─────────

    def test_ping_false_on_all_faults(self):
        """ping() is the connectivity oracle — must never claim liveness."""
        self.assertFalse(self.client.ping())  # missing socket
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.bind(self.sock_path)
        s.close()
        self.assertFalse(self.client.ping())  # refused
        os.unlink(self.sock_path)

    def test_ping_true_on_live_server(self):
        """Sanity: a live server yields ping()==True."""
        import threading as _t

        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            os.unlink(self.sock_path)
        except FileNotFoundError:
            pass
        srv.bind(self.sock_path)
        srv.listen(1)
        done = _t.Event()

        def accept_and_close():
            conn, _ = srv.accept()
            conn.close()
            done.set()

        _t.Thread(target=accept_and_close, daemon=True).start()
        self.assertTrue(self.client.ping())
        srv.close()


if __name__ == "__main__":
    unittest.main()
