"""Tests for the stale daemon.pid / lock-file cleanup (`healthcheck --clean-stale`).

Hermetic by design: every path the cleanup resolves (pid file, lock dir,
socket, instance lock) is patched into a per-test temp directory. The conftest
production-path guard must stay silent — if it fires, cleanup is reaching into
live state.
"""

import io
import json
import os
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from toolrecall import daemon  # noqa: E402
from toolrecall import healthcheck as hc  # noqa: E402

# Above Linux' pid_max ceiling (2**22), so /proc/<pid> can never exist.
DEAD_PID = "99999999"


def _held_lock(path):
    """Open *path* and flock it, returning the open file object (POSIX only)."""
    import fcntl

    fh = open(path, "a+")
    fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    return fh


def _health(**over):
    base = {
        "procs": 1,
        "pid_file": "",
        "pid_alive": False,
        "lock_files": 0,
        "socket_path": "/tmp/tr.sock",
        "socket": True,
        "proxy": True,
        "proxy_port": 8569,
        "shim_state": "inactive",
        "hit_rate": None,
        "log_file": "",
        "log_errors_recent": 0,
        "log_warnings_recent": 0,
    }
    base.update(over)
    return base


class CleanupCase(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory(prefix="tr_stale_")
        self.dir = self._td.name
        self.pid_file = os.path.join(self.dir, "daemon.pid")
        self.sock = os.path.join(self.dir, "toolrecall.sock")
        self._patchers = [
            patch.object(hc, "_pid_file", return_value=self.pid_file),
            patch.object(hc, "_default_socket_path", return_value=self.sock),
        ]
        for p in self._patchers:
            p.start()

    def tearDown(self):
        for p in reversed(self._patchers):
            p.stop()
        self._td.cleanup()

    def write_pid(self, content):
        with open(self.pid_file, "w") as f:
            f.write(content)

    def lock(self, name):
        return os.path.join(self.dir, name)

    def leftovers(self):
        return sorted(n for n in os.listdir(self.dir) if n.endswith(".lck"))


class TestProcState(CleanupCase):
    def test_own_pid_is_alive(self):
        self.assertEqual(hc._proc_state(os.getpid()), "alive")
        self.assertTrue(hc._pid_alive(os.getpid()))

    def test_unused_pid_is_dead(self):
        self.assertEqual(hc._proc_state(int(DEAD_PID)), "dead")
        self.assertFalse(hc._pid_alive(int(DEAD_PID)))

    def test_invalid_pid_is_dead(self):
        self.assertEqual(hc._proc_state(0), "dead")
        self.assertEqual(hc._proc_state(-1), "dead")

    @unittest.skipUnless(hasattr(os, "fork"), "needs fork()")
    def test_zombie_is_not_alive(self):
        pid = os.fork()
        if pid == 0:  # child: exit without the parent reaping it
            os._exit(0)
        try:
            deadline = time.time() + 5
            while hc._proc_state(pid) != "zombie" and time.time() < deadline:
                time.sleep(0.02)
            self.assertEqual(hc._proc_state(pid), "zombie", "child never became a zombie")
            self.assertFalse(hc._pid_alive(pid))
        finally:
            os.waitpid(pid, 0)


class TestStalePidReason(CleanupCase):
    def test_dead_pid_reason(self):
        reason = hc._stale_pid_reason(DEAD_PID)
        self.assertIsNotNone(reason)
        self.assertIn("not alive", reason or "")

    def test_live_toolrecall_pid_is_not_stale(self):
        with patch.object(hc, "_is_toolrecall_process", return_value=True):
            self.assertIsNone(hc._stale_pid_reason(str(os.getpid())))

    def test_reused_pid_is_stale(self):
        with patch.object(hc, "_is_toolrecall_process", return_value=False):
            reason = hc._stale_pid_reason(str(os.getpid()))
        self.assertIsNotNone(reason)
        self.assertIn("reused", reason or "")

    def test_unreadable_cmdline_is_not_proof(self):
        with patch.object(hc, "_is_toolrecall_process", return_value=None):
            self.assertIsNone(hc._stale_pid_reason(str(os.getpid())))

    def test_garbage_content_is_stale(self):
        reason = hc._stale_pid_reason("not-a-pid")
        self.assertIsNotNone(reason)
        self.assertIn("invalid", reason or "")

    def test_empty_content_is_not_stale(self):
        self.assertIsNone(hc._stale_pid_reason("  "))


class TestCleanup(CleanupCase):
    def test_removes_dead_pid_file(self):
        self.write_pid(DEAD_PID)
        report = hc.cleanup_stale_state()
        self.assertTrue(report["pid_file_removed"])
        self.assertIn("not alive", report["pid_file_reason"])
        self.assertFalse(os.path.exists(self.pid_file))

    def test_keeps_live_pid_file(self):
        self.write_pid(str(os.getpid()))
        with patch.object(hc, "_is_toolrecall_process", return_value=True):
            report = hc.cleanup_stale_state()
        self.assertFalse(report["pid_file_removed"])
        self.assertEqual(report["pid_file_reason"], "")
        self.assertTrue(os.path.exists(self.pid_file))

    def test_removes_pid_file_reused_by_foreign_process(self):
        self.write_pid(str(os.getpid()))
        with patch.object(hc, "_is_toolrecall_process", return_value=False):
            report = hc.cleanup_stale_state()
        self.assertTrue(report["pid_file_removed"])
        self.assertIn("reused", report["pid_file_reason"])

    def test_removes_garbage_pid_file(self):
        self.write_pid("garbage\n")
        report = hc.cleanup_stale_state()
        self.assertTrue(report["pid_file_removed"])
        self.assertIn("invalid", report["pid_file_reason"])

    def test_absent_pid_file_is_a_noop(self):
        report = hc.cleanup_stale_state()
        self.assertFalse(report["pid_file_removed"])
        self.assertEqual(report["pid_file_reason"], "")
        self.assertEqual(report["lock_files_removed"], [])

    def test_removes_orphaned_lock_files(self):
        self.write_pid(DEAD_PID)
        a = self.lock("daemon-aaa.lck")
        b = self.lock("daemon-bbb.lck")
        for path in (a, b):
            with open(path, "w") as f:
                f.write("424242")
        with open(self.lock("not-a-lock.txt"), "w") as f:
            f.write("keep me")
        report = hc.cleanup_stale_state()
        self.assertEqual(sorted(report["lock_files_removed"]), sorted([a, b]))
        self.assertEqual(report["lock_files_kept"], 0)
        self.assertEqual(os.listdir(self.dir).count("not-a-lock.txt"), 1)

    @unittest.skipUnless(os.name == "posix", "flock is POSIX-only")
    def test_keeps_lock_held_by_live_process(self):
        held = self.lock("daemon-held.lck")
        fh = _held_lock(held)
        try:
            report = hc.cleanup_stale_state()
        finally:
            fh.close()
        self.assertEqual(report["lock_files_removed"], [])
        self.assertEqual(report["lock_files_kept"], 1)
        self.assertTrue(os.path.exists(held))

    @unittest.skipUnless(os.name == "posix", "flock is POSIX-only")
    def test_does_not_remove_pid_while_instance_lock_is_held(self):
        self.write_pid(DEAD_PID)
        gate = daemon._instance_lock_path(self.sock)
        fh = _held_lock(gate)
        try:
            report = hc.cleanup_stale_state()
        finally:
            fh.close()
            if os.path.exists(gate):
                os.unlink(gate)
        self.assertFalse(report["pid_file_removed"])
        self.assertIn("instance lock", report["pid_file_reason"])
        self.assertTrue(os.path.exists(self.pid_file))

    def test_cleanup_does_not_leave_its_own_gate_lock(self):
        self.write_pid(DEAD_PID)
        report = hc.cleanup_stale_state()
        self.assertTrue(report["pid_file_removed"])
        self.assertEqual(self.leftovers(), [])

    def test_second_cleanup_is_idempotent(self):
        self.write_pid(DEAD_PID)
        self.assertTrue(hc.cleanup_stale_state()["pid_file_removed"])
        second = hc.cleanup_stale_state()
        self.assertFalse(second["pid_file_removed"])
        self.assertEqual(second["pid_file_reason"], "")


class TestReportAndRun(CleanupCase):
    def test_line_reports_removal(self):
        self.write_pid(DEAD_PID)
        line = hc.cleanup_line(hc.cleanup_stale_state())
        self.assertTrue(line.startswith("cleanup: removed stale daemon.pid"))
        self.assertIn("no orphaned lock files", line)

    def test_line_reports_kept_gate(self):
        self.write_pid(str(os.getpid()))
        with patch.object(hc, "_is_toolrecall_process", return_value=True):
            line = hc.cleanup_line(hc.cleanup_stale_state())
        self.assertIn("no stale daemon.pid", line)

    def test_run_clean_stale_prints_cleanup_first(self):
        self.write_pid(DEAD_PID)
        buf = io.StringIO()
        with (
            patch.object(hc, "gather", return_value=_health()),
            redirect_stdout(buf),
        ):
            code = hc.run(clean_stale=True)
        lines = buf.getvalue().splitlines()
        self.assertEqual(code, 0)
        self.assertTrue(lines[0].startswith("cleanup: removed stale daemon.pid"))

    def test_run_json_includes_cleanup(self):
        self.write_pid(DEAD_PID)
        buf = io.StringIO()
        with (
            patch.object(hc, "gather", return_value=_health()),
            redirect_stdout(buf),
        ):
            code = hc.run(as_json=True, clean_stale=True)
        payload = json.loads(buf.getvalue())
        self.assertEqual(code, 0)
        self.assertTrue(payload["cleanup"]["pid_file_removed"])
        self.assertIn("health", payload)

    def test_run_without_flag_reports_no_cleanup(self):
        buf = io.StringIO()
        with (
            patch.object(hc, "gather", return_value=_health()),
            redirect_stdout(buf),
        ):
            hc.run(as_json=True)
        self.assertNotIn("cleanup", json.loads(buf.getvalue()))


class TestCliFlag(unittest.TestCase):
    def test_clean_stale_flag_is_forwarded(self):
        from toolrecall import cli

        with (
            patch.object(sys, "argv", ["toolrecall", "healthcheck", "--clean-stale"]),
            patch.object(hc, "run", return_value=0) as run_mock,
            self.assertRaises(SystemExit),
        ):
            cli.cmd_healthcheck()
        run_mock.assert_called_once_with(as_json=False, clean_stale=True)

    def test_unknown_flag_exits_two(self):
        from toolrecall import cli

        with (
            patch.object(sys, "argv", ["toolrecall", "healthcheck", "--nope"]),
            self.assertRaises(SystemExit) as ctx,
            redirect_stderr(io.StringIO()),
        ):
            cli.cmd_healthcheck()
        self.assertEqual(ctx.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
