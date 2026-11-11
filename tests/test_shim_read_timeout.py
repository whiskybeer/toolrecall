"""RED-first tests for the shim read-path budget and circuit breaker (P2).

Defect: ``cached_read`` ran inside the caller's ``open()`` with transport's 5 s
default and no breaker. A busy daemon therefore charged every read 5 s (19 WebUI
requests stalled 5.5-5.8 s in 7 days, thread stack in ``recv_exact``) and charged
the next read again.

Contract under test:
    * a stalled daemon costs ONE short budget (~0.5 s), and the caller still gets
      the real file content;
    * every timeout is counted for ``toolrecall stats``;
    * after 3 consecutive timeouts the shim stops connecting entirely;
    * when the cooldown expires exactly one probe is allowed (half-open), and a
      failed probe re-opens the breaker immediately;
    * any successful read resets the streak.
"""

import os
import socket
import tempfile
import threading
import time
import unittest
from unittest import mock

import toolrecall.client as client_mod
import toolrecall.shim as shim_mod
import toolrecall.shim_timeouts as timeouts_mod


class _StalledDaemon:
    """A UDS server that accepts and then never replies — a busy daemon."""

    def __init__(self, path: str):
        self.path = path
        self.accepts = 0
        self._conns = []
        self._stop = threading.Event()
        self._srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._srv.bind(path)
        self._srv.listen(16)
        self._srv.settimeout(0.05)
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        while not self._stop.is_set():
            try:
                conn, _ = self._srv.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            self.accepts += 1
            self._conns.append(conn)  # hold it open, never answer

    def close(self):
        self._stop.set()
        self._thread.join(timeout=2)
        for c in self._conns:
            try:
                c.close()
            except OSError:
                pass
        try:
            self._srv.close()
        except OSError:
            pass
        try:
            os.unlink(self.path)
        except OSError:
            pass


class _Base(unittest.TestCase):
    BUDGET_MS = 300

    def setUp(self):
        # The config below sets exclude_prefixes = [], so the agent-home default
        # cannot swallow these reads — no need to pin a non-TMPDIR location.
        self.tmp = tempfile.mkdtemp(prefix="tr_p2_")
        self.addCleanup(self._rmtree)
        cfg = os.path.join(self.tmp, "tr.toml")
        with open(cfg, "w") as f:
            f.write("[shim]\nexclude_prefixes = []\n")
        self._saved_env = {
            k: os.environ.get(k) for k in ("TOOLRECALL_CONFIG", "TOOLRECALL_SHIM_READ_TIMEOUT_MS")
        }
        os.environ["TOOLRECALL_CONFIG"] = cfg
        os.environ.pop("TOOLRECALL_SHIM_READ_TIMEOUT_MS", None)
        self.addCleanup(self._restore_env)
        shim_mod._SKIP_PREFIXES = None
        shim_mod._breaker.update({"timeouts": 0, "open_until": 0.0, "recovering": False})
        # Never leak an open breaker into the next test module: process-global
        # state left "open" would silently bypass the shim for every later test.
        self.addCleanup(
            shim_mod._breaker.update,
            {"timeouts": 0, "open_until": 0.0, "recovering": False},
        )

        budget = mock.patch.object(shim_mod, "_READ_TIMEOUT_DEFAULT_MS", self.BUDGET_MS)
        budget.start()
        self.addCleanup(budget.stop)

        self.log = os.path.join(self.tmp, "shim_timeouts.jsonl")
        log_path = mock.patch.object(timeouts_mod, "_log_path", lambda: self.log)
        log_path.start()
        self.addCleanup(log_path.stop)

        self.daemon = _StalledDaemon(os.path.join(self.tmp, "daemon.sock"))
        self.addCleanup(self.daemon.close)
        self._saved_path = client_mod._DEFAULT_PATH
        self._saved_client = client_mod._client
        client_mod._DEFAULT_PATH = self.daemon.path
        client_mod._client = None
        self.addCleanup(self._restore_client)

    def _rmtree(self):
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def _restore_env(self):
        for k, v in self._saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shim_mod._SKIP_PREFIXES = None

    def _restore_client(self):
        client_mod._DEFAULT_PATH = self._saved_path
        client_mod._client = self._saved_client

    def _file(self, body: str = "payload\n") -> str:
        p = os.path.join(self.tmp, f"f{len(os.listdir(self.tmp))}.txt")
        with open(p, "w") as f:
            f.write(body)
        return p

    def _read(self, path: str) -> str:
        fh = shim_mod._shim_open(path, "r")
        try:
            return fh.read()
        finally:
            fh.close()


class BudgetAndFallbackTest(_Base):
    def test_a_stalled_daemon_costs_one_budget_and_still_returns_the_file(self):
        p = self._file("hello\n")
        t0 = time.monotonic()
        content = self._read(p)
        elapsed = time.monotonic() - t0
        self.assertEqual(content, "hello\n")
        self.assertLess(elapsed, 1.0)
        self.assertGreaterEqual(elapsed, self.BUDGET_MS / 1000.0 * 0.5)
        self.assertGreaterEqual(self.daemon.accepts, 1)

    def test_every_timeout_is_counted_for_stats(self):
        self._read(self._file())
        self._read(self._file())
        summary = timeouts_mod.summary()
        self.assertEqual(summary["total"], 2)
        self.assertEqual(summary["last_24h"], 2)
        self.assertIsNotNone(summary["last"])

    def test_shim_passes_the_short_budget_to_cached_read(self):
        seen = {}

        def _read(path, source="agent_tool", timeout=None):
            seen["timeout"] = timeout
            return {"cached": False}

        with mock.patch.object(shim_mod, "_get_tr", lambda: {"read": _read, "send": None}):
            self._read(self._file())
        self.assertEqual(seen["timeout"], self.BUDGET_MS / 1000.0)

    def test_env_override_sets_the_budget(self):
        with mock.patch.dict(os.environ, {"TOOLRECALL_SHIM_READ_TIMEOUT_MS": "1500"}):
            self.assertAlmostEqual(shim_mod._read_timeout_s(), 1.5)

    def test_bad_env_values_fall_back_to_the_default(self):
        for bad in ("nope", "0", "-5"):
            with mock.patch.dict(os.environ, {"TOOLRECALL_SHIM_READ_TIMEOUT_MS": bad}):
                self.assertAlmostEqual(shim_mod._read_timeout_s(), self.BUDGET_MS / 1000.0, msg=bad)


class BreakerTest(_Base):
    def test_three_timeouts_open_the_breaker_and_further_reads_make_no_connection(self):
        seen = []
        for _ in range(3):
            self.assertEqual(self._read(self._file()), "payload\n")
            seen.append(self.daemon.accepts)
        self.assertEqual(seen, sorted(seen), f"accept count went backwards: {seen}")
        self.assertGreaterEqual(seen[-1], 3, "every stalled read must attempt the daemon")
        self.assertGreater(shim_mod._breaker["open_until"], 0.0)

        before = self.daemon.accepts
        t0 = time.monotonic()
        for i in range(3):
            self.assertEqual(self._read(self._file(f"after{i}\n")), f"after{i}\n")
        elapsed = time.monotonic() - t0
        self.assertEqual(self.daemon.accepts, before, "breaker-open reads must not connect")
        self.assertLess(elapsed, 0.6, "3 breaker-open reads must not each wait for a socket")

    def test_half_open_probes_once_and_reopens_when_the_probe_fails(self):
        for _ in range(3):
            self._read(self._file())
        before = self.daemon.accepts

        # Simulate the cooldown window elapsing.
        shim_mod._breaker["open_until"] = time.monotonic() - 0.01
        self._read(self._file())
        self.assertGreater(self.daemon.accepts, before, "cooldown must allow a probe")
        # The probe timed out → breaker re-opened at threshold 1, not 3.
        self.assertGreater(shim_mod._breaker["open_until"], time.monotonic())

        # And the next read is blocked again without paying another timeout.
        after_probe = self.daemon.accepts
        self._read(self._file())
        self.assertEqual(self.daemon.accepts, after_probe)

    def test_a_successful_read_resets_the_streak(self):
        seq = [
            {"_daemon_timeout": True},
            {"_daemon_timeout": True},
            {"cached": False},  # daemon answered → streak resets
            {"_daemon_timeout": True},
            {"_daemon_timeout": True},
        ]
        it = iter(seq)

        def _read(path, source="agent_tool", timeout=None):
            return next(it)

        with mock.patch.object(shim_mod, "_get_tr", lambda: {"read": _read, "send": None}):
            for _ in seq:
                self._read(self._file())
        self.assertEqual(shim_mod._breaker["open_until"], 0.0, "2+2 < threshold 3")
        self.assertEqual(shim_mod._breaker["timeouts"], 2)

    def test_breaker_never_blocks_a_local_hit(self):
        # Trip the breaker, then confirm the in-process cache still serves.
        for _ in range(3):
            self._read(self._file())
        before = self.daemon.accepts
        entry_path = self._file("local\n")
        entry = shim_mod._local_store(entry_path, "local\n", True, "agent_tool")
        if entry is None:  # local store may be disabled in this config
            self.skipTest("local store disabled")
        self.assertEqual(self._read(entry_path), "local\n")
        self.assertEqual(self.daemon.accepts, before, "local hit must not touch the daemon")


class ClientMarkerTest(unittest.TestCase):
    def test_client_tags_a_daemon_timeout_for_the_breaker(self):
        fake_client = mock.Mock()
        fake_client.send.return_value = {"error": "daemon_timeout"}
        fake_direct = mock.Mock()
        fake_direct.cached_read.return_value = {"cached": False, "content": "x"}
        cfg = mock.Mock(mcp_allowed_paths=[])

        with (
            mock.patch.object(client_mod, "_get_client", lambda: fake_client),
            mock.patch.object(client_mod, "_get_direct_cache", lambda: fake_direct),
            mock.patch("toolrecall.config.load_config", lambda *a, **k: cfg),
        ):
            out = client_mod.cached_read("/tmp/x.txt", source="shim", timeout=0.05)

        self.assertTrue(out["_daemon_timeout"])
        self.assertEqual(fake_client.send.call_args.kwargs["timeout"], 0.05)

    def test_client_without_a_timeout_keeps_the_admin_default(self):
        fake_client = mock.Mock()
        fake_client.send.return_value = {"cached": True, "content": "ok"}
        with mock.patch.object(client_mod, "_get_client", lambda: fake_client):
            client_mod.cached_read("/tmp/x.txt")
        self.assertNotIn("timeout", fake_client.send.call_args.kwargs)


if __name__ == "__main__":
    unittest.main()
