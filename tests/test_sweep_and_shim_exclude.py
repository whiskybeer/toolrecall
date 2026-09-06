"""Stale instance-lock file sweep + shim virtual-FS exclusion.

Covers two fixes shipped together:

1. ``daemon._sweep_stale_lock_files`` — startup sweep unlinks
   ``daemon-*.lck`` orphans whose holder PID is dead, and never touches
   locks held by a live PID. (Background: SIGKILL/kill-path teardown
   leaves the lock FILE behind even though the flock is OS-released; the
   healthcheck then warns on the accumulation.)

2. ``shim`` virtual-FS exclusion — ``/proc/``, ``/sys/``, ``/dev/`` reads
   bypass the shim by default (procfs/sysfs are generated live and can
   never produce a cache hit; intercepting them just burns an RPC that
   ends in a daemon path-denial warning).
"""

import os
import tempfile
import unittest
from unittest import mock

from toolrecall import daemon as daemon_mod
from toolrecall import shim as shim_mod
from toolrecall import config as config_mod


class SweepStaleLockFilesTest(unittest.TestCase):
    """_sweep_stale_lock_files removes dead-holder lck files, keeps live ones."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="tr_sweep_test_")
        self.lock_dir = self._tmp.name
        # _sweep_stale_lock_files derives the dir from PID_FILE's parent —
        # point it at the temp dir.
        self._pid_file_patcher = mock.patch.object(
            daemon_mod, "PID_FILE", os.path.join(self.lock_dir, "daemon.pid")
        )
        self._pid_file_patcher.start()
        self.addCleanup(self._pid_file_patcher.stop)
        self.addCleanup(self._tmp.cleanup)

    def _write_lck(self, name: str, pid: str) -> str:
        path = os.path.join(self.lock_dir, name)
        with open(path, "w") as f:
            f.write(pid)
        return path

    def test_sweep_removes_dead_pid_lock(self):
        # PID 99999999 is above pid_max on every realistic kernel — dead.
        dead = self._write_lck("daemon-dead000000000001.lck", "99999999")
        self.assertTrue(os.path.exists(dead))
        removed = daemon_mod._sweep_stale_lock_files()
        self.assertEqual(removed, 1)
        self.assertFalse(os.path.exists(dead))

    def test_sweep_keeps_live_pid_lock(self):
        live = self._write_lck("daemon-live00000000001.lck", str(os.getpid()))
        removed = daemon_mod._sweep_stale_lock_files()
        self.assertEqual(removed, 0)
        self.assertTrue(os.path.exists(live))

    def test_sweep_keeps_own_production_lock(self):
        # The currently-running daemon's file (this test process) must
        # survive: same scenario as the production daemon during its own
        # restart sweep.
        own = self._write_lck("daemon-own000000000001.lck", str(os.getpid()))
        daemon_mod._sweep_stale_lock_files()
        self.assertTrue(os.path.exists(own))

    def test_sweep_removes_lock_with_garbage_pid(self):
        # Empty or non-numeric holder = never successfully locked — orphan.
        garbage = self._write_lck("daemon-garb00000000001.lck", "")
        garbage2 = self._write_lck("daemon-garb00000000002.lck", "not-a-pid")
        removed = daemon_mod._sweep_stale_lock_files()
        self.assertEqual(removed, 2)
        self.assertFalse(os.path.exists(garbage))
        self.assertFalse(os.path.exists(garbage2))

    def test_sweep_ignores_non_lck_files(self):
        keep = os.path.join(self.lock_dir, "daemon.pid")
        with open(keep, "w") as f:
            f.write(str(os.getpid()))
        other = os.path.join(self.lock_dir, "daemon-other.txt")
        with open(other, "w") as f:
            f.write("99999999")
        daemon_mod._sweep_stale_lock_files()
        self.assertTrue(os.path.exists(keep))
        self.assertTrue(os.path.exists(other))

    def test_run_daemon_sweep_never_blocks_start_on_sweep_error(self):
        # If the sweep raises, run_daemon must continue to the lock
        # acquisition (not crash). Simulate by sweeping an unreadable dir.
        with mock.patch.object(
            daemon_mod,
            "_sweep_stale_lock_files",
            side_effect=OSError("injected"),
        ):
            # run_daemon would sys.exit(0) if the lock is held; we only need
            # to prove it reaches _acquire_instance_lock without propagating
            # the injected OSError.
            with mock.patch.object(
                daemon_mod,
                "_acquire_instance_lock",
                return_value=object(),
            ) as acquire:
                # Stop before the heavy startup path: after lock acquisition
                # run_daemon builds DaemonServer — patch that out too.
                with mock.patch.object(
                    daemon_mod, "DaemonServer", side_effect=RuntimeError("stop-here")
                ):
                    with self.assertRaises(RuntimeError):
                        daemon_mod.run_daemon(
                            socket_path=os.path.join(self.lock_dir, "t.sock"),
                            foreground=True,
                        )
                acquire.assert_called_once()


class ShimVirtualFsExclusionTest(unittest.TestCase):
    """/proc//sys//dev/ are excluded from shim interception by default."""

    def setUp(self):
        # Reset the module-level cache so each test reloads from config.
        self._saved = shim_mod._SKIP_PREFIXES
        shim_mod._SKIP_PREFIXES = None
        self.addCleanup(setattr, shim_mod, "_SKIP_PREFIXES", self._saved)

    def test_config_default_includes_virtual_fs(self):
        cfg = config_mod.Config()  # defaults only — no user toml needed
        prefixes = cfg.shim_exclude_prefixes
        for p in ("/proc/", "/sys/", "/dev/"):
            self.assertIn(p, prefixes, f"{p} missing from default exclude list")

    def test_should_skip_proc_paths(self):
        self.assertTrue(shim_mod._should_skip("/proc/self/status"))
        self.assertTrue(shim_mod._should_skip("/proc/meminfo"))
        self.assertTrue(shim_mod._should_skip("/proc/1234/task"))
        self.assertTrue(shim_mod._should_skip("/sys/kernel/mm"))
        self.assertTrue(shim_mod._should_skip("/dev/null"))

    def test_should_not_skip_normal_paths(self):
        self.assertFalse(shim_mod._should_skip("/home/hermes/workspace/x.py"))
        self.assertFalse(shim_mod._should_skip("/tmp/notes.txt"))
        self.assertFalse(shim_mod._should_skip("/procself/impostor"))  # not /proc/
        self.assertFalse(shim_mod._should_skip("/system/real"))

    def test_relative_paths_never_match_absolute_prefixes(self):
        # os.fspath passes relative paths through unchanged; they can't
        # start with "/proc/" and must go through the shim.
        self.assertFalse(shim_mod._should_skip("proc/self/status"))

    def test_env_var_override_still_works(self):
        # User-set exclude list replaces the default entirely (normalized).
        cfg = mock.MagicMock()
        cfg.shim_exclude_prefixes = ["/proc"]
        with mock.patch("toolrecall.config.load_config", return_value=cfg):
            shim_mod._load_skip_prefixes()
        # Normalized to trailing slash.
        self.assertEqual(shim_mod._SKIP_PREFIXES, ["/proc/"])
        self.assertTrue(shim_mod._should_skip("/proc/self/status"))

    def test_bare_slash_entry_is_dropped(self):
        # A misconfigured "/" would bypass the shim for everything — refuse.
        cfg = mock.MagicMock()
        cfg.shim_exclude_prefixes = ["/"]
        with mock.patch("toolrecall.config.load_config", return_value=cfg):
            shim_mod._load_skip_prefixes()
        self.assertEqual(shim_mod._SKIP_PREFIXES, [])

    def test_config_load_failure_falls_back_to_defaults(self):
        with mock.patch(
            "toolrecall.config.load_config",
            side_effect=RuntimeError("injected config failure"),
        ):
            shim_mod._load_skip_prefixes()
        self.assertEqual(shim_mod._SKIP_PREFIXES, ["/proc/", "/sys/", "/dev/"])
        self.assertTrue(shim_mod._should_skip("/proc/self/status"))


if __name__ == "__main__":
    unittest.main()
