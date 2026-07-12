"""Shared E2E test helper — manages a ToolRecall daemon subprocess.

Usage:
    with E2EDaemon() as daemon:
        daemon.wait_until_ready()
        # ... run tests against the daemon ...
        # daemon.stop() is called automatically on __exit__
"""

import os
import sys
import time
import tempfile
import subprocess
from pathlib import Path
from typing import Optional

from toolrecall.transport import TransportClient


class E2EDaemon:
    """Manage a ToolRecall daemon subprocess for integration tests.

    Creates a temporary directory for the daemon's socket and database,
    starts the daemon as a subprocess, and provides a context manager
    interface for clean teardown.

    Attributes:
        socket_path: Path to the daemon's Unix domain socket.
        db_path:     Path to the daemon's SQLite cache database.
        tmpdir:      Temporary directory (deleted on stop).
        process:     The subprocess.Popen handle (None until started).
    """

    def __init__(
        self,
        socket_path: Optional[str] = None,
        db_path: Optional[str] = None,
    ):
        self._tmpdir: Optional[tempfile.TemporaryDirectory] = None
        self.process: Optional[subprocess.Popen] = None
        self._client: Optional[TransportClient] = None

        if socket_path is not None:
            self.socket_path = socket_path
        else:
            self._tmpdir = tempfile.TemporaryDirectory(prefix="toolrecall_e2e_")
            self.socket_path = os.path.join(self._tmpdir.name, "toolrecall.sock")

        if db_path is not None:
            self.db_path = db_path
        else:
            if self._tmpdir is None:
                self._tmpdir = tempfile.TemporaryDirectory(prefix="toolrecall_e2e_")
            self.db_path = os.path.join(self._tmpdir.name, "cache.db")

    # ── Lifecycle ──────────────────────────────────────────

    def start(self) -> None:
        """Launch the daemon subprocess.

        The daemon is started with ``foreground=True`` so it runs in the
        calling process tree.  The ``TOOLRECALL_CACHE_DB`` environment
        variable is set so the daemon opens the correct test database.

        Blocks until the daemon is ready to accept connections.
        """
        if self.process is not None:
            return  # already started

        # Ensure parent directories exist
        Path(self.socket_path).parent.mkdir(parents=True, exist_ok=True)

        env = os.environ.copy()
        env["TOOLRECALL_CACHE_DB"] = self.db_path

        code = (
            "from toolrecall.daemon import run_daemon; "
            f"run_daemon(socket_path={self.socket_path!r}, foreground=True)"
        )

        self.process = subprocess.Popen(
            [sys.executable, "-c", code],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        self._client = TransportClient(path=self.socket_path)
        self._wait_until_ready()

    def _wait_until_ready(self, timeout: float = 10.0) -> None:
        """Block until the daemon responds to a ping, or raise TimeoutError."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                if self._client is not None and self._client.ping(timeout=1.0):
                    return
            except Exception:
                pass

            # Check whether the subprocess already exited (crash on start)
            if self.process is not None and self.process.poll() is not None:
                rc = self.process.returncode
                stdout, stderr = self.process.communicate()
                raise RuntimeError(
                    f"Daemon exited prematurely (returncode={rc}).\n"
                    f"stdout:\n{stdout.decode()}\n"
                    f"stderr:\n{stderr.decode()}"
                )

            time.sleep(0.1)

        raise TimeoutError(
            f"Daemon did not become ready within {timeout}s "
            f"(socket_path={self.socket_path}, db_path={self.db_path})"
        )

    def wait_until_ready(self, timeout: float = 10.0) -> None:
        """Public convenience wrapper around _wait_until_ready."""
        self._wait_until_ready(timeout=timeout)

    def stop(self) -> None:
        """Terminate the daemon subprocess and clean up the socket file."""
        # Terminate the subprocess
        if self.process is not None and self.process.poll() is None:
            try:
                self.process.terminate()
                self.process.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=3.0)

        self.process = None
        self._client = None

        # Clean up socket file
        if os.path.exists(self.socket_path):
            try:
                os.unlink(self.socket_path)
            except OSError:
                pass

        # Clean up socket parent dir if it's our temp dir
        if self._tmpdir is not None:
            try:
                self._tmpdir.cleanup()
            except OSError:
                pass
            self._tmpdir = None

    # ── Properties ─────────────────────────────────────────

    @property
    def running(self) -> bool:
        """Check whether the daemon subprocess is still alive."""
        if self.process is None:
            return False
        return self.process.poll() is None

    @property
    def client(self) -> TransportClient:
        """Return the TransportClient (must call start() first)."""
        if self._client is None:
            raise RuntimeError("E2EDaemon has not been started yet")
        return self._client

    # ── Context manager ────────────────────────────────────

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.stop()
        return False  # do not suppress exceptions
