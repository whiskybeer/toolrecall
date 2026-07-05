"""E2E: CLI commands via subprocess.

Tests:
  - toolrecall daemon --foreground : creates socket file and is reachable
"""

import os
import subprocess
import sys
import time
import unittest
import tempfile


class TestE2ECLI(unittest.TestCase):
    """CLI commands via subprocess."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="tr_e2e_cli_")
        self.socket_path = os.path.join(self._tmpdir, "tr_cli.sock")
        self.db_path = os.path.join(self._tmpdir, "tr_cli_cache.db")
        self._env = os.environ.copy()
        self._env["TOOLRECALL_CACHE_DB"] = self.db_path
        self._daemon_proc = None

    def tearDown(self):
        if self._daemon_proc and self._daemon_proc.poll() is None:
            self._daemon_proc.terminate()
            try:
                self._daemon_proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._daemon_proc.kill()
                self._daemon_proc.wait()

    def _repo_root(self):
        return os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

    def test_cli_daemon_foreground_creates_socket(self):
        """Daemon --foreground creates the socket file and responds to ping."""
        script = (
            "import sys; sys.path.insert(0, '" + self._repo_root() + "'); "
            "from toolrecall.daemon import run_daemon; "
            f"run_daemon(socket_path='{self.socket_path}', foreground=True)"
        )
        self._daemon_proc = subprocess.Popen(
            [sys.executable, "-c", script],
            cwd=self._repo_root(),
            env=self._env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        # Wait until socket exists or timeout
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if os.path.exists(self.socket_path):
                # Socket exists — try pinging
                import socket
                import struct
                import json
                try:
                    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    s.settimeout(2)
                    s.connect(self.socket_path)
                    payload = json.dumps({"cmd": "ping"}).encode()
                    s.sendall(struct.pack("!I", len(payload)) + payload)
                    raw_len = s.recv(4)
                    msg_len = struct.unpack("!I", raw_len)[0]
                    data = s.recv(msg_len)
                    resp = json.loads(data)
                    s.close()
                    if resp.get("pong"):
                        return  # All good
                except Exception:
                    pass
            time.sleep(0.1)
        self.fail(f"Daemon did not become ready at {self.socket_path} within 5s")