"""Tests for toolrecall.healthcheck — the generalized daemon/cache healthcheck.

Hermetic by design: no daemon is spawned; we exercise the pure gather/warnings/
render logic with tiny shims of the real path helpers.
"""

import os
import tempfile
import unittest
from unittest.mock import patch

import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from toolrecall import healthcheck as hc  # noqa: E402


def _mk_health(**over):
    base = {
        "procs": 1,
        "pid_file": "1234",
        "pid_alive": True,
        "lock_files": 1,
        "socket_path": "/tmp/tr.sock",
        "socket": True,
        "shim_state": "active",
        "hit_rate": 0.98,
        "log_file": "",
        "log_errors_recent": 0,
        "log_warnings_recent": 0,
    }
    base.update(over)
    return base


class TestWarnings(unittest.TestCase):
    def test_healthy_no_warnings(self):
        self.assertEqual(hc.warnings(_mk_health()), [])

    def test_zero_procs_warns_daemon_down(self):
        w = hc.warnings(_mk_health(procs=0, pid_alive=False))
        self.assertTrue(any("NOT running" in x for x in w))

    def test_many_procs_warns_leak(self):
        w = hc.warnings(_mk_health(procs=3))
        self.assertTrue(any("expected 1" in x for x in w))

    def test_stale_pid_warns(self):
        w = hc.warnings(_mk_health(pid_alive=False))
        self.assertTrue(any("stale daemon.pid" in x for x in w))

    def test_missing_pid_file_warns(self):
        w = hc.warnings(_mk_health(pid_file=""))
        self.assertTrue(any("no daemon.pid" in x for x in w))

    def test_lock_leak_warns(self):
        w = hc.warnings(_mk_health(lock_files=10))
        self.assertTrue(any("lock files" in x for x in w))

    def test_missing_socket_warns(self):
        w = hc.warnings(_mk_health(socket=False))
        self.assertTrue(any("socket" in x for x in w))

    def test_negative_procs_unknown(self):
        w = hc.warnings(_mk_health(procs=-1))
        self.assertTrue(any("unknown" in x for x in w))

    def test_log_errors_warn(self):
        w = hc.warnings(_mk_health(log_errors_recent=3, log_file="/x/toolrecall.log"))
        self.assertTrue(any("ERROR" in x for x in w))

    def test_log_noisy_warnings_warn(self):
        w = hc.warnings(_mk_health(log_warnings_recent=25, log_file="/x/toolrecall.log"))
        self.assertTrue(any("WARNING" in x for x in w))

    def test_log_quiet_no_warning(self):
        self.assertEqual(hc.warnings(_mk_health(log_warnings_recent=3, log_file="/x/l.log")), [])


class TestLogScan(unittest.TestCase):
    def test_scan_counts_levels(self):
        with tempfile.TemporaryDirectory() as td:
            log = os.path.join(td, "toolrecall.log")
            with open(log, "w") as f:
                f.write("2026-09-05 10:00:00 INFO toolrecall.daemon req=1 ok\n")
                f.write("2026-09-05 10:00:01 ERROR toolrecall.daemon boom\n")
                f.write("2026-09-05 10:00:02 WARNING toolrecall.db blocked\n")
                f.write("no level anchor here\n")
            with patch.dict(os.environ, {"TOOLRECALL_LOG_FILE": log}):
                path, errs, warns = hc._scan_recent_log()
            self.assertEqual(path, log)
            self.assertEqual(errs, 1)
            self.assertEqual(warns, 1)

    def test_scan_missing_file_is_zero(self):
        with patch.dict(os.environ, {"TOOLRECALL_LOG_FILE": "/nonexistent/x.log"}):
            path, errs, warns = hc._scan_recent_log()
        self.assertEqual((path, errs, warns), ("", 0, 0))

    def test_scan_bounded_tail(self):
        with tempfile.TemporaryDirectory() as td:
            log = os.path.join(td, "toolrecall.log")
            with open(log, "w") as f:
                # ERROR far in the past (> tail window), INFO recent
                f.write("ERROR old\n")
                f.write("x" * 70_000 + "\n")
                f.write("INFO recent\n")
            with patch.dict(
                os.environ, {"TOOLRECALL_LOG_TAIL_KB": "2", "TOOLRECALL_LOG_FILE": log}
            ):
                _path, errs, _warns = hc._scan_recent_log()
            self.assertEqual(errs, 0)  # old ERROR outside 2KB tail window


class TestGather(unittest.TestCase):
    @patch.object(hc, "_default_socket_path", return_value="/tmp/tr.sock")
    @patch.object(hc, "count_daemon_procs", return_value=-1)
    def test_lock_count_reads_dir(self, _procs, _sock):
        with tempfile.TemporaryDirectory() as td:
            for n in ("daemon-abc123.lck", "daemon-x.lck", "other.txt"):
                open(os.path.join(td, n), "w").close()
            with patch.object(hc, "_pid_file", return_value=os.path.join(td, "daemon.pid")):
                info = hc.gather()
                # socket path override → but socket file absent in tmpdir
                self.assertEqual(info["lock_files"], 2)
                self.assertEqual(info["procs"], -1)


class TestRunExit(unittest.TestCase):
    @patch("sys.argv", ["toolrecall", "healthcheck"])
    @patch.object(hc, "gather", return_value=_mk_health())
    def test_healthy_exits_zero(self, _g):
        self.assertEqual(hc.run(), 0)

    @patch("sys.argv", ["toolrecall", "healthcheck"])
    @patch.object(hc, "gather", return_value=_mk_health(procs=0, pid_alive=False))
    def test_daemon_down_exits_one(self, _g):
        self.assertEqual(hc.run(), 1)


if __name__ == "__main__":
    unittest.main()
