"""Hermetic daemons must never touch the production PID file.

Regression (2026-09-10): a pytest e2e daemon on
``/tmp/toolrecall_e2e_*/toolrecall.sock`` lost the global :8569 bind race and
exited — but not before writing its own (soon dead) PID into the shared
``~/.toolrecall/daemon.pid``. ``toolrecall healthcheck`` and
``daemon --stop`` then pointed at a non-existent process while the real daemon
(7076) was fine.

The rule: only the daemon that owns the PRODUCTION socket reads/writes/removes
that file. Classification = tempdir exemption (same as tests/conftest.py's
production-path guard), never ``transport._default_socket_path()`` (which honors
TOOLRECALL_TRANSPORT and would call a test socket "production").
"""

import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import toolrecall.daemon as daemon  # noqa: E402

# Production-state guard: this module once unlinked the live daemon's socket
# file (see test_production_shutdown_still_cleans_its_pid_file). Fail loudly
# instead of silently breaking every path-based client.
PROD_SOCKET = f"/run/user/{os.getuid()}/toolrecall.sock"
_prod_socket_before = None


def setUpModule():
    global _prod_socket_before
    _prod_socket_before = os.path.exists(PROD_SOCKET)


def tearDownModule():
    now = os.path.exists(PROD_SOCKET)
    assert now == _prod_socket_before, (
        "test module changed PRODUCTION socket state: "
        f"{PROD_SOCKET} existed={_prod_socket_before} before, exists={now} after"
    )


class TestPidFileGateClassification(unittest.TestCase):
    def test_tempdir_sockets_are_hermetic(self):
        self.assertFalse(daemon._should_manage_pid_file("/tmp/toolrecall_e2e_x/toolrecall.sock"))
        self.assertFalse(
            daemon._should_manage_pid_file(
                os.path.join(tempfile.gettempdir(), "tr_sweep_test_abc", "t.sock")
            )
        )

    def test_production_socket_paths_own_the_pid_file(self):
        uid = os.getuid()
        self.assertTrue(daemon._should_manage_pid_file(f"/run/user/{uid}/toolrecall.sock"))
        self.assertTrue(
            daemon._should_manage_pid_file(os.path.expanduser("~/.toolrecall/toolrecall.sock"))
        )

    def test_non_path_transport_keeps_legacy_behavior(self):
        # Windows: the PID file IS the stop mechanism — must not be gated off.
        self.assertTrue(daemon._should_manage_pid_file("tcp://127.0.0.1:8568"))

    def test_empty_socket_never_writes(self):
        self.assertFalse(daemon._should_manage_pid_file(None))
        self.assertFalse(daemon._should_manage_pid_file(""))


class TestShutdownPidCleanupGate(unittest.TestCase):
    """_handle_shutdown must not delete a PID file it never owned."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="tr_pidgate_")
        self.pid_file = os.path.join(self.tmpdir, "daemon.pid")
        self._pid_patch = patch.object(daemon, "PID_FILE", self.pid_file)
        self._pid_patch.start()
        self.addCleanup(self._pid_patch.stop)
        # _do_exit SIGTERMs the process — never let it run under pytest.
        self._exit_patch = patch.object(daemon.DaemonServer, "_do_exit", lambda self: None)
        self._exit_patch.start()
        self.addCleanup(self._exit_patch.stop)

    def _server(self, socket_path):
        srv = object.__new__(daemon.DaemonServer)
        srv.socket_path = socket_path
        return srv

    def test_hermetic_shutdown_leaves_production_pid_file_alone(self):
        with open(self.pid_file, "w") as f:
            f.write("7076")
        sock = os.path.join(self.tmpdir, "toolrecall.sock")
        daemon.DaemonServer._handle_shutdown(self._server(sock))
        self.assertTrue(
            os.path.exists(self.pid_file),
            "hermetic daemon must not remove the production PID file",
        )
        with open(self.pid_file) as f:
            self.assertEqual(f.read(), "7076")

    def test_production_shutdown_still_cleans_its_pid_file(self):
        # NEVER point this at the real production socket: _handle_shutdown
        # UNLINKS self.socket_path, so using /run/user/<uid>/toolrecall.sock
        # deletes the live daemon's socket file (it keeps serving the orphaned
        # inode, so the breakage is silent until the next client connects).
        # Patch the classifier instead — the behavior under test is the PID
        # removal, not the path classification (covered above).
        with open(self.pid_file, "w") as f:
            f.write(str(os.getpid()))
        sock = os.path.join(self.tmpdir, "toolrecall.sock")
        with open(sock, "w") as f:
            f.write("")  # stand-in for the socket file the handler unlinks
        srv = self._server(sock)
        with patch.object(daemon, "_should_manage_pid_file", return_value=True):
            daemon.DaemonServer._handle_shutdown(srv)
        self.assertFalse(os.path.exists(self.pid_file))
        self.assertFalse(os.path.exists(sock))  # handler unlinked its own socket


class TestHermeticDaemonEndToEnd(unittest.TestCase):
    """The real regression: spawn a daemon on a temp socket, prove the shared
    PID file is byte-identical before and after its full lifecycle."""

    def test_temp_socket_daemon_never_writes_production_pid(self):
        prod_pid = daemon.PID_FILE
        before = _read_or_none(prod_pid)
        tmpdir = tempfile.mkdtemp(prefix="tr_pidgate_e2e_")
        sock = os.path.join(tmpdir, "toolrecall.sock")
        env = dict(os.environ)
        env.update(
            {
                "TOOLRECALL_TRANSPORT": sock,
                "TOOLRECALL_UDS_PATH": sock,
                "TOOLRECALL_CACHE_DB": os.path.join(tmpdir, "cache.db"),
                # Don't fight the production proxy for :8569, and don't spend
                # 13.5s retrying: fail the bind immediately (loudly, by design).
                "TOOLRECALL_FORWARD_BIND_ATTEMPTS": "1",
                "TOOLRECALL_FORWARD_BIND_DELAY": "0",
            }
        )
        proc = subprocess.Popen(
            [sys.executable, "-m", "toolrecall", "daemon", "--foreground"],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            deadline = time.time() + 20
            while time.time() < deadline and not os.path.exists(sock):
                if proc.poll() is not None:
                    self.fail(f"daemon exited early with code {proc.returncode}")
                time.sleep(0.2)
            self.assertTrue(os.path.exists(sock), "daemon never bound its temp socket")
            # The temp socket is live → the production PID file must be untouched.
            self.assertEqual(
                _read_or_none(prod_pid),
                before,
                "hermetic daemon overwrote the production PID file while starting",
            )
        finally:
            if proc.poll() is None:
                proc.send_signal(signal.SIGTERM)
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)
        # ... and again after a graceful shutdown, which used to unlink it.
        self.assertEqual(
            _read_or_none(prod_pid),
            before,
            "hermetic daemon removed the production PID file on shutdown",
        )


def _read_or_none(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None


if __name__ == "__main__":
    unittest.main()
