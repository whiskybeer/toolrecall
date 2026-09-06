"""Regression tests for the Windows handover bugs (0.8.19, 2026-09-04).

Covers:
1. `_ensure_agent_integration` no longer raises UnboundLocalError when the
   `claude` binary is absent but `~/.claude.json` exists (the fallback branch
   used to reference `claude_multiplexer_only` before it was ever assigned).
2. `_acquire_instance_lock` on Windows now returns a real lock handle via a
   byte-range lock (msvcrt on Windows) instead of unconditionally returning
   None. On POSIX this test pins the SEMANTICS the Windows branch mirrors:
   second acquire blocked, release on close, PID recorded in the lock file.
   The Windows branch itself is exercised structurally (see
   test_windows_branch_not_a_stub).
3. `cmd_restart` no longer crashes with FileNotFoundError when `systemctl`
   is absent (Windows / minimal containers) — it falls back to a direct
   daemon restart instead.
4. `main()` reconfigures stdout/stderr to UTF-8 (errors=replace) on win32 so
   emoji/box-drawing output survives cp1252 consoles.
5. `cmd_setup` on Windows installs a scheduled task (no longer a no-op).

Mocking notes:
- `shutil`/`subprocess` are imported INSIDE the cli functions, so patch the
  stdlib modules themselves (`shutil.which`, `subprocess.run`), not
  `toolrecall.cli.<mod>`.
- `expanduser` is called on the module-global `toolrecall.cli.os` — patch
  `toolrecall.cli.os.path.expanduser`.
- `_ensure_daemon` is patched so no real daemon/socket is touched.
"""

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import toolrecall.cli as cli
import toolrecall.daemon as daemon


def _expand_to(home):
    """expanduser that maps '~/...' into the temp home (hermetic)."""

    def _expand(p):
        if p == "~":
            return home
        if p.startswith("~/"):
            return os.path.join(home, p[2:])
        return p

    return patch.object(cli.os.path, "expanduser", _expand)


def _no_daemon_touch():
    """Patch out everything that would reach the real daemon socket.

    - cli._ensure_daemon → True (no daemon spawn)
    - toolrecall.daemon.stop_daemon → no-op
    - toolrecall.transport.TransportClient → ping answers immediately
    """
    tc = patch(
        "toolrecall.transport.TransportClient",
        lambda *a, **k: type(
            "TC", (), {"send": staticmethod(lambda m: {"pong": True, "pid": 1})}
        )(),
    )
    return [
        patch.object(cli, "_ensure_daemon", return_value=True),
        patch("toolrecall.daemon.stop_daemon"),
        tc,
    ]


class TestClaudeMultiplexerOnlyUnboundLocal(unittest.TestCase):
    """Bug 1: setup crashed when `claude` is not on PATH but ~/.claude.json exists."""

    def test_fallback_branch_no_unboundlocal(self):
        with tempfile.TemporaryDirectory() as home:
            # Stub ~/.claude.json so the fallback branch fires.
            with open(os.path.join(home, ".claude.json"), "w") as f:
                json.dump({"mcpServers": {}}, f)

            argv_patch = patch.object(sys, "argv", ["toolrecall", "setup", "--yes"])
            # claude absent everywhere, hermes absent (skip shim branch)
            which_patch = patch.object(shutil, "which", return_value=None)
            home_patch = _expand_to(home)

            with argv_patch, which_patch, home_patch:
                result = cli._ensure_agent_integration()

            self.assertTrue(result.get("claude"), "claude fallback should report success")
            # The MCP config must have been merged into the stubbed ~/.claude.json.
            with open(os.path.join(home, ".claude.json")) as f:
                cfg = json.load(f)
            self.assertIn("toolrecall", cfg["mcpServers"])

    def test_noninteractive_claude_bin_defaults_full(self):
        """claude present + non-interactive → multiplexer_only stays False (no crash)."""
        with tempfile.TemporaryDirectory() as home:
            fake_claude = os.path.join(home, "bin", "claude")
            os.makedirs(os.path.dirname(fake_claude), exist_ok=True)
            open(fake_claude, "w").close()

            def fake_which(name):
                return fake_claude if name == "claude" else None

            argv_patch = patch.object(sys, "argv", ["toolrecall", "setup"])
            which_patch = patch.object(shutil, "which", side_effect=fake_which)
            env_patch = patch.dict(os.environ, {"TOOLRECALL_NONINTERACTIVE": "1"})
            run_patch = patch.object(
                subprocess, "run", return_value=subprocess.CompletedProcess([], 0)
            )
            home_patch = _expand_to(home)

            with argv_patch, which_patch, env_patch, run_patch, home_patch:
                result = cli._ensure_agent_integration()

            self.assertTrue(result.get("claude"))


class TestWindowsLockSemantics(unittest.TestCase):
    """Bug 2: the daemon single-instance lock must be a real lock on all OSes.

    The Windows branch now mirrors the POSIX fcntl semantics below using
    msvcrt.locking. These tests pin the contract on POSIX (where the suite
    runs); the Windows branch is exercised structurally by source check.
    """

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="tr_test_winlock_")
        daemon.PID_FILE = os.path.join(self.tmpdir, "daemon.pid")
        self.sock_a = os.path.join(self.tmpdir, "a.sock")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_lock_contract_holds(self):
        """Acquire → block second → close → re-acquire; PID recorded."""
        fh1 = daemon._acquire_instance_lock(self.sock_a)
        self.assertIsNotNone(fh1)
        try:
            fh2 = daemon._acquire_instance_lock(self.sock_a)
            self.assertIsNone(fh2, "second daemon on same socket must be blocked")
            lock_path = daemon._instance_lock_path(self.sock_a)
            with open(lock_path) as f:
                self.assertEqual(f.read().strip(), str(os.getpid()), "PID recorded")
        finally:
            fh1.close()
        fh3 = daemon._acquire_instance_lock(self.sock_a)
        self.assertIsNotNone(fh3, "re-acquirable after close")
        fh3.close()

    def test_windows_branch_not_a_stub(self):
        """Source-level guard: the IS_WINDOWS branch must contain a real lock
        (msvcrt.locking), not `return None`. msvcrt.LK_NBLCK is the byte-range
        lock that mirrors fcntl.flock semantics on Windows."""
        import inspect

        src = inspect.getsource(daemon._acquire_instance_lock)
        self.assertIn("msvcrt.locking", src)
        self.assertIn("LK_NBLCK", src)


class TestRestartWithoutSystemd(unittest.TestCase):
    """Bug 3: cmd_restart on Windows (no systemctl) must fall back, not crash."""

    def test_restart_falls_back_without_systemctl(self):
        buf = io.StringIO()
        argv_patch = patch.object(sys, "argv", ["toolrecall", "restart"])
        which_patch = patch.object(shutil, "which", return_value=None)
        out_patch = patch.object(sys, "stdout", buf)
        with tempfile.TemporaryDirectory() as tmp:
            expand_patch = _expand_to(tmp)
            guards = _no_daemon_touch()
            with argv_patch, which_patch, out_patch, expand_patch, guards[0], guards[1], guards[2]:
                cli.cmd_restart()  # must not raise FileNotFoundError

        out = buf.getvalue()
        self.assertIn("Falling back to direct daemon restart", out)
        self.assertIn("Daemon started via fallback", out)

    def test_restart_with_systemd_success_path(self):
        """systemctl present + exit 0 → no fallback, readiness ping runs."""
        buf = io.StringIO()
        fake_result = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        argv_patch = patch.object(sys, "argv", ["toolrecall", "restart"])
        which_patch = patch.object(shutil, "which", return_value="/usr/bin/systemctl")
        run_patch = patch.object(subprocess, "run", return_value=fake_result)
        out_patch = patch.object(sys, "stdout", buf)
        with tempfile.TemporaryDirectory() as tmp:
            expand_patch = _expand_to(tmp)
            guards = _no_daemon_touch()
            with (
                argv_patch,
                which_patch,
                run_patch,
                out_patch,
                expand_patch,
                guards[0],
                guards[1],
                guards[2],
            ):
                cli.cmd_restart()

        out = buf.getvalue()
        self.assertIn("Restarting via systemd --user", out)
        self.assertNotIn("Falling back", out)


class TestUtf8Reconfigure(unittest.TestCase):
    """Bug 4: main() reconfigures streams to UTF-8 on win32."""

    def test_win32_reconfigures_stdout(self):
        calls = []

        class FakeStream(io.StringIO):
            def reconfigure(self, **kw):
                calls.append(kw)

        argv_patch = patch.object(sys, "argv", [])
        plat_patch = patch.object(sys, "platform", "win32")
        out_patch = patch.object(sys, "stdout", FakeStream())
        with argv_patch, plat_patch, out_patch:
            cli.main()

        self.assertEqual(calls, [{"encoding": "utf-8", "errors": "replace"}])

    def test_posix_does_not_reconfigure(self):
        calls = []

        class FakeStream(io.StringIO):
            def reconfigure(self, **kw):
                calls.append(kw)

        argv_patch = patch.object(sys, "argv", [])
        plat_patch = patch.object(sys, "platform", "linux")
        out_patch = patch.object(sys, "stdout", FakeStream())
        with argv_patch, plat_patch, out_patch:
            cli.main()

        self.assertEqual(calls, [], "no reconfigure on POSIX")

    def test_stream_without_reconfigure_is_tolerated(self):
        """A stream lacking reconfigure (captured output) must not crash main()."""
        argv_patch = patch.object(sys, "argv", [])
        plat_patch = patch.object(sys, "platform", "win32")
        out_patch = patch.object(sys, "stdout", io.StringIO())  # no reconfigure
        with argv_patch, plat_patch, out_patch:
            cli.main()  # must not raise


class TestWindowsAutostartSetup(unittest.TestCase):
    """Bug 5 (recommendation): cmd_setup on Windows installs a scheduled task."""

    def test_windows_setup_calls_scheduled_task(self):
        buf = io.StringIO()
        seen_cmds = []

        def fake_run(cmd, **kwargs):
            seen_cmds.append(cmd)
            return subprocess.CompletedProcess([], 0, stdout="", stderr="")

        with tempfile.TemporaryDirectory() as home:
            fake_bin = os.path.join(home, "bin", "toolrecall.exe")
            os.makedirs(os.path.dirname(fake_bin), exist_ok=True)
            open(fake_bin, "w").close()

            def fake_which(name):
                return fake_bin if name == "toolrecall" else None

            argv_patch = patch.object(sys, "argv", ["toolrecall", "setup", "--yes"])
            which_patch = patch.object(shutil, "which", side_effect=fake_which)
            run_patch = patch.object(subprocess, "run", side_effect=fake_run)
            out_patch = patch.object(sys, "stdout", buf)
            expand_patch = _expand_to(home)
            plat_patch = patch.object(sys, "platform", "win32")
            guards = _no_daemon_touch()
            with (
                argv_patch,
                which_patch,
                run_patch,
                out_patch,
                expand_patch,
                plat_patch,
                guards[0],
                guards[1],
                guards[2],
            ):
                cli.cmd_setup()

        out = buf.getvalue()
        schtasks_calls = [c for c in seen_cmds if c and c[0] == "schtasks"]
        self.assertTrue(schtasks_calls, "schtasks /Create must be invoked")
        self.assertIn("ONLOGSTART", schtasks_calls[0])
        self.assertIn("Windows autostart", out)


if __name__ == "__main__":
    unittest.main()
