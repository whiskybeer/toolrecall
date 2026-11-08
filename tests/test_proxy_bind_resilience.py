"""Forward-proxy bind resilience + healthcheck visibility.

Root cause (2026-09-10 18:22): a daemon restart raced the previous process's
socket teardown. `run_forward_proxy` hit EADDRINUSE, logged one ERROR and
returned — permanently. The daemon stayed "active" with NO listener on :8569,
so every LLM call routed through the proxy failed with APIConnectionError
(gateway log: "API call failed (attempt 3/3): APIConnectionError"), and no
health signal mentioned the proxy at all.

These tests pin both halves: the bind retries while the port is held, and the
healthcheck reports the listener state.
"""

import os
import socket
import sys
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import toolrecall.healthcheck as hc  # noqa: E402
import toolrecall.proxy as proxy_mod  # noqa: E402


def _hold_port(listen: bool = True) -> tuple[socket.socket, int]:
    """Occupy an ephemeral port and return (socket, port)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", 0))
    if listen:
        s.listen(1)
    return s, s.getsockname()[1]


class _StubServer:
    """Stand-in for ThreadedHTTPServer that fails N times, then binds."""

    fail_times = 0
    attempts_seen = 0
    last_kwargs = None

    def __init__(self, addr, handler):  # noqa: ARG002 - mirrors real signature
        type(self).attempts_seen += 1
        if type(self).attempts_seen <= type(self).fail_times:
            raise OSError(98, "Address already in use")
        self.server_address = addr
        self.server_port = addr[1]

    def serve_forever(self):
        pass

    def close(self):
        pass


class TestBindRetry(unittest.TestCase):
    def setUp(self):
        _StubServer.fail_times = 0
        _StubServer.attempts_seen = 0
        self._env = patch.dict(
            os.environ,
            {
                "TOOLRECALL_FORWARD_BIND_ATTEMPTS": "5",
                "TOOLRECALL_FORWARD_BIND_DELAY": "0.01",
            },
        )
        self._env.start()
        self.addCleanup(self._env.stop)

    def test_binds_immediately_when_port_free(self):
        with patch.object(proxy_mod, "ThreadedHTTPServer", _StubServer):
            srv = proxy_mod._bind_proxy_server("127.0.0.1", 0, proxy_mod.ForwardProxyHandler)
        self.assertIsNotNone(srv)
        self.assertEqual(_StubServer.attempts_seen, 1)

    def test_retries_while_port_is_held_then_succeeds(self):
        # The outage case: first attempts hit EADDRINUSE, then the old
        # process releases it and the bind succeeds — instead of giving up.
        _StubServer.fail_times = 3
        with patch.object(proxy_mod, "ThreadedHTTPServer", _StubServer):
            srv = proxy_mod._bind_proxy_server("127.0.0.1", 0, proxy_mod.ForwardProxyHandler)
        self.assertIsNotNone(srv, "bind must retry, not give up on the race")
        self.assertEqual(_StubServer.attempts_seen, 4)

    def test_returns_none_after_exhausting_attempts(self):
        _StubServer.fail_times = 99  # never frees
        with patch.object(proxy_mod, "ThreadedHTTPServer", _StubServer):
            srv = proxy_mod._bind_proxy_server("127.0.0.1", 0, proxy_mod.ForwardProxyHandler)
        self.assertIsNone(srv)
        self.assertEqual(_StubServer.attempts_seen, 5)

    def test_non_eaddrinuse_errors_still_propagate(self):
        class _Boom(_StubServer):
            def __init__(self, addr, handler):  # noqa: ARG002
                raise OSError(13, "Permission denied")

        with patch.object(proxy_mod, "ThreadedHTTPServer", _Boom):
            with self.assertRaises(OSError):
                proxy_mod._bind_proxy_server("127.0.0.1", 0, proxy_mod.ForwardProxyHandler)

    def test_real_socket_released_mid_retry_is_picked_up(self):
        # End-to-end with real sockets: hold the port, start the bind in a
        # thread, release after a beat — the retry must win. Uses its own
        # (longer) retry budget than setUp's fast default, since the holder is
        # only released after 0.1s.
        holder, port = _hold_port(listen=False)
        result = {}

        def _bind():
            result["srv"] = proxy_mod._bind_proxy_server(
                "127.0.0.1", port, proxy_mod.ForwardProxyHandler
            )

        with patch.dict(
            os.environ,
            {
                "TOOLRECALL_FORWARD_BIND_ATTEMPTS": "20",
                "TOOLRECALL_FORWARD_BIND_DELAY": "0.05",
            },
        ):
            t = threading.Thread(target=_bind)
            t.start()
            time.sleep(0.1)
            holder.close()
            t.join(timeout=10)
        srv = result.get("srv")
        self.assertIsNotNone(srv, "real bind must recover once the port is released")
        if srv is not None:
            srv.server_close()


class TestHealthcheckProxySignal(unittest.TestCase):
    def test_proxy_listening_false_when_port_dead(self):
        _s, port = _hold_port()
        port_free = port
        _s.close()
        time.sleep(0.05)
        self.assertFalse(hc.proxy_listening(port_free, timeout=0.5))

    def test_proxy_listening_true_when_port_bound(self):
        holder, port = _hold_port(listen=True)
        try:
            self.assertTrue(hc.proxy_listening(port, timeout=0.5))
        finally:
            holder.close()

    def test_warning_names_the_proxy_when_not_listening(self):
        info: hc.HealthInfo = {
            "procs": 1,
            "pid_file": "123",
            "pid_alive": True,
            "lock_files": 0,
            "socket_path": "/tmp/x.sock",
            "socket": True,
            "proxy": False,
            "proxy_port": 8569,
            "shim_state": "inactive",
            "hit_rate": None,
            "log_file": "",
            "log_errors_recent": 0,
            "log_warnings_recent": 0,
        }
        notes = hc.warnings(info)
        self.assertTrue(
            any("forward proxy NOT listening" in n for n in notes),
            f"proxy outage must be named in warnings, got: {notes}",
        )

    def test_no_proxy_warning_when_listening(self):
        info: hc.HealthInfo = {
            "procs": 1,
            "pid_file": "123",
            "pid_alive": True,
            "lock_files": 0,
            "socket_path": "/tmp/x.sock",
            "socket": True,
            "proxy": True,
            "proxy_port": 8569,
            "shim_state": "inactive",
            "hit_rate": None,
            "log_file": "",
            "log_errors_recent": 0,
            "log_warnings_recent": 0,
        }
        self.assertFalse(
            any("forward proxy" in n for n in hc.warnings(info)),
            "healthy listener must not produce a proxy warning",
        )


if __name__ == "__main__":
    unittest.main()
