"""Tests for transport.py — Unix Domain Socket (UDS) and TCP IPC layer.

Covers:
  - _default_socket_path() returns correct paths for POSIX vs Windows
  - _is_tcp() detection and _parse_tcp() parsing
  - create_socket() creates correct socket family (AF_UNIX vs AF_INET)
  - bind_socket() and connect_socket() lifecycle
  - Framed message protocol: send_message / receive_message round-trip
  - TransportClient.send() request/response with a real server
  - TransportClient.ping() connectivity check
  - Timeout behavior on unavailable daemon
  - Large message handling (near 1MB limit)
"""

import json
import os
import socket
import struct
import sys
import threading
import time
import unittest
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from toolrecall.transport import (
    _default_socket_path, _is_tcp, _parse_tcp,
    create_socket, bind_socket, connect_socket,
    send_message, receive_message,
    TransportClient, DEFAULT_PATH,
)


class TestSocketPath(unittest.TestCase):
    """_default_socket_path() returns correct paths per platform."""

    def test_posix_default_no_xdg(self):
        """Without XDG_RUNTIME_DIR, path resolves to ~/.toolrecall/toolrecall.sock."""
        old_xdg = os.environ.pop("XDG_RUNTIME_DIR", None)
        os.environ.pop("TOOLRECALL_PORT", None)
        try:
            path = _default_socket_path()
            self.assertFalse(_is_tcp(path), "POSIX default should be UDS, not TCP")
            self.assertTrue(path.endswith("toolrecall.sock"), f"Unexpected path: {path}")
            self.assertIn(".toolrecall", path)
        finally:
            if old_xdg:
                os.environ["XDG_RUNTIME_DIR"] = old_xdg

    def test_posix_default_with_xdg(self):
        """With XDG_RUNTIME_DIR set, socket sits under that dir."""
        old_xdg = os.environ.pop("XDG_RUNTIME_DIR", None)
        os.environ["XDG_RUNTIME_DIR"] = "/run/user/1000"
        try:
            path = _default_socket_path()
            self.assertIn("/run/user/1000/toolrecall.sock", path)
        finally:
            if old_xdg:
                os.environ["XDG_RUNTIME_DIR"] = old_xdg
            else:
                os.environ.pop("XDG_RUNTIME_DIR", None)

    def test_windows_default_returns_tcp(self):
        """On Windows, default path is tcp://127.0.0.1:8568."""
        import toolrecall.transport as tp
        old_platform = tp.IS_WINDOWS
        tp.IS_WINDOWS = True
        try:
            path = _default_socket_path()
            self.assertTrue(_is_tcp(path), "Windows default should be TCP")
            host, port = _parse_tcp(path)
            self.assertEqual(host, "127.0.0.1")
            self.assertEqual(port, 8568)
        finally:
            tp.IS_WINDOWS = old_platform


class TestTCPHelpers(unittest.TestCase):
    """_is_tcp() and _parse_tcp() helper functions."""

    def test_is_tcp_true(self):
        self.assertTrue(_is_tcp("tcp://127.0.0.1:8568"))

    def test_is_tcp_false(self):
        self.assertFalse(_is_tcp("/tmp/toolrecall.sock"))

    def test_parse_tcp_standard(self):
        host, port = _parse_tcp("tcp://127.0.0.1:8568")
        self.assertEqual(host, "127.0.0.1")
        self.assertEqual(port, 8568)

    def test_parse_tcp_custom_port(self):
        host, port = _parse_tcp("tcp://0.0.0.0:9090")
        self.assertEqual(host, "0.0.0.0")
        self.assertEqual(port, 9090)


class TestSocketLifecycle(unittest.TestCase):
    """create_socket, bind_socket, connect_socket work together via UDS."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.sock_path = os.path.join(self.tmpdir, "test.sock")

    def tearDown(self):
        try:
            os.unlink(self.sock_path)
        except FileNotFoundError:
            pass

    def test_create_and_bind_unix_socket(self):
        """Create UDS socket, bind it, verify file exists with 0o700 perms."""
        sock = create_socket(self.sock_path)
        self.assertIsNotNone(sock)
        self.assertEqual(sock.family, socket.AF_UNIX)
        bind_socket(sock, self.sock_path)
        self.assertTrue(os.path.exists(self.sock_path))
        # Check permissions
        perms = os.stat(self.sock_path).st_mode & 0o777
        self.assertEqual(perms, 0o700)
        sock.close()

    def test_create_and_bind_tcp_socket(self):
        """Create TCP socket, bind to loopback, verify port is listening."""
        path = "tcp://127.0.0.1:0"  # port 0 = OS-assigned
        sock = create_socket(path)
        self.assertEqual(sock.family, socket.AF_INET)
        bind_socket(sock, path)
        bound_port = sock.getsockname()[1]
        self.assertGreater(bound_port, 0)
        sock.close()

    def test_connect_to_bound_unix_socket(self):
        """Connect to a bound UDS socket completes without error."""
        server = create_socket(self.sock_path)
        bind_socket(server, self.sock_path)
        server.listen(1)

        client = create_socket(self.sock_path)
        connect_socket(client, self.sock_path)
        client.close()
        server.close()


class TestFramedMessageProtocol(unittest.TestCase):
    """send_message / receive_message with length-prefixed JSON framing.

    Protocol: 4-byte big-endian length prefix + UTF-8 JSON payload.
    """

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.sock_path = os.path.join(self.tmpdir, "proto.sock")

    def tearDown(self):
        try:
            os.unlink(self.sock_path)
        except FileNotFoundError:
            pass

    def _server_thread(self, response_data: dict):
        """Run a server that accepts one connection, reads, replies, closes."""
        server = create_socket(self.sock_path)
        bind_socket(server, self.sock_path)
        server.listen(1)
        conn, _ = server.accept()
        req = receive_message(conn)
        self._received = req
        send_message(conn, response_data)
        conn.close()
        server.close()

    def test_round_trip(self):
        """Send a request, receive a response — both framed correctly."""
        self._received = None
        response = {"status": "ok", "data": [1, 2, 3]}
        t = threading.Thread(target=self._server_thread, args=(response,))
        t.start()
        time.sleep(0.1)

        client = create_socket(self.sock_path)
        connect_socket(client, self.sock_path)
        send_message(client, {"cmd": "ping", "id": 42})
        resp = receive_message(client)
        client.close()
        t.join()

        self.assertIsNotNone(resp)
        self.assertEqual(resp["status"], "ok")
        self.assertEqual(self._received["cmd"], "ping")
        self.assertEqual(self._received["id"], 42)

    def test_receive_none_on_close(self):
        """If connection closes without data, receive_message returns None."""
        server = create_socket(self.sock_path)
        bind_socket(server, self.sock_path)
        server.listen(1)
        t = threading.Thread(target=lambda: (
            setattr(self, '_conn', server.accept()[0]),
            time.sleep(0.05),
            self._conn.close(),
        ))
        t.start()
        time.sleep(0.1)

        client = create_socket(self.sock_path)
        connect_socket(client, self.sock_path)
        time.sleep(0.2)
        resp = receive_message(client)
        client.close()
        t.join()

        self.assertIsNone(resp, "Should return None on closed connection")

    def test_large_message_within_limit(self):
        """A message near the 1MB limit (100KB test) is sent and received."""
        large_data = {"data": "x" * (100 * 1024)}
        self._received = None
        t = threading.Thread(target=self._server_thread, args=(large_data,))
        t.start()
        time.sleep(0.1)

        client = create_socket(self.sock_path)
        connect_socket(client, self.sock_path)
        send_message(client, {"cmd": "large_test"})
        resp = receive_message(client)
        client.close()
        t.join()

        self.assertIsNotNone(resp)
        self.assertEqual(len(resp["data"]), 100 * 1024)

    def test_message_too_large_returns_error(self):
        """Messages exceeding _MAX_MSG_SIZE are rejected with an error dict."""
        import toolrecall.transport as tp
        old_limit = tp._MAX_MSG_SIZE
        tp._MAX_MSG_SIZE = 100  # Artificially low for testing
        try:
            server = create_socket(self.sock_path)
            bind_socket(server, self.sock_path)
            server.listen(1)
            conn_ref = [None]

            def accept_one():
                conn, _ = server.accept()
                conn_ref[0] = conn
                resp = receive_message(conn)
                conn_ref[0].close()

            t = threading.Thread(target=accept_one)
            t.start()
            time.sleep(0.1)

            client = create_socket(self.sock_path)
            connect_socket(client, self.sock_path)
            # Send a payload larger than 100 bytes
            payload = json.dumps({"data": "x" * 200}).encode("utf-8")
            client.sendall(struct.pack("!I", len(payload)) + payload)
            time.sleep(0.2)
            client.close()
            t.join()
        finally:
            tp._MAX_MSG_SIZE = old_limit
            server.close()


class TestTransportClient(unittest.TestCase):
    """TransportClient.send() and .ping() with a real server process."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.sock_path = os.path.join(self.tmpdir, "client_test.sock")

    def tearDown(self):
        try:
            os.unlink(self.sock_path)
        except FileNotFoundError:
            pass

    def _start_server(self):
        """Background server that echoes any request."""
        server = create_socket(self.sock_path)
        bind_socket(server, self.sock_path)
        server.listen(5)
        threading.Thread(target=lambda: self._serve_loop(server), daemon=True).start()
        return server

    def _serve_loop(self, server):
        while True:
            try:
                conn, _ = server.accept()
                req = receive_message(conn)
                if req is None:
                    conn.close()
                    continue
                # Echo back with "result" wrapping
                send_message(conn, {"result": req, "echo": True})
                conn.close()
            except Exception:
                break

    def test_client_send_receives_response(self):
        """TransportClient.send() sends a dict and gets a response back."""
        self._start_server()
        time.sleep(0.1)
        client = TransportClient(self.sock_path)
        resp = client.send({"cmd": "ping"})
        self.assertIsNotNone(resp)
        self.assertTrue(resp.get("echo"))
        self.assertEqual(resp["result"]["cmd"], "ping")

    def test_client_ping_succeeds(self):
        """ping() returns True when daemon is reachable."""
        self._start_server()
        time.sleep(0.1)
        client = TransportClient(self.sock_path)
        self.assertTrue(client.ping())

    def test_client_ping_fails_when_no_daemon(self):
        """ping() returns False when no daemon is listening."""
        client = TransportClient(self.sock_path)
        self.assertFalse(client.ping())

    def test_client_send_returns_daemon_unavailable(self):
        """send() returns {'error': 'daemon_unavailable'} when no daemon."""
        client = TransportClient(self.sock_path)
        resp = client.send({"cmd": "ping"})
        self.assertEqual(resp.get("error"), "daemon_unavailable")

    def test_client_timeout_on_slow_server(self):
        """send() times out gracefully when server doesn't respond."""
        # Bind but never accept
        server = create_socket(self.sock_path)
        bind_socket(server, self.sock_path)
        server.listen(1)
        # Don't accept — client will timeout
        client = TransportClient(self.sock_path)
        resp = client.send({"cmd": "ping"}, timeout=0.5)
        self.assertIn("error", resp)
        server.close()

    def test_custom_path_property(self):
        """TransportClient stores and exposes the path."""
        client = TransportClient(self.sock_path)
        self.assertEqual(client.path, self.sock_path)
        self.assertFalse(client.is_tcp)

    def test_is_tcp_property(self):
        """is_tcp returns True for TCP paths."""
        client = TransportClient("tcp://127.0.0.1:9999")
        self.assertTrue(client.is_tcp)


if __name__ == "__main__":
    unittest.main()
