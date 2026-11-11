"""RED-first tests for the shim probe state machine (P1).

Defect: ``gather()`` mapped every probe failure — including a *timeout* — to
``shim_state="inactive"``, so a working shim was reported as missing and the
operator was told to reinstall it. A timeout is absence of evidence, not
evidence of absence.

Contract under test:
    active    applied: yes  AND  probe: pass
    inactive  the probe ran and found no working shim (reason says why)
    unknown   the probe did not complete (timeout / spawn error)
"""

import os
import subprocess
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from toolrecall import healthcheck as hc  # noqa: E402

PROBE_CMD = ["toolrecall", "shim", "--status", "--all"]


def _mk_info(**over):
    base = {
        "procs": 1,
        "pid_file": "1234",
        "pid_alive": True,
        "lock_files": 1,
        "socket_path": "/tmp/tr.sock",
        "socket": True,
        "proxy": True,
        "proxy_port": 8899,
        "shim_state": "active",
        "shim_reason": "",
        "shim_probe_s": 1.0,
        "hit_rate": None,
        "log_file": "",
        "log_errors_recent": 0,
        "log_warnings_recent": 0,
    }
    base.update(over)
    return base


def _probe_out(applied: bool, passing: bool) -> str:
    """Render `shim --status --all` output the way cli.py:1694-1700 does."""
    return (
        "✅ /tmp/venv\n"
        "     python: /tmp/venv/bin/python\n"
        f"     package: ok | shim: {'present' if applied else 'missing'} | "
        f"applied: {'yes' if applied else 'no'} | "
        f"probe: {'pass' if passing else 'FAIL'}\n"
        "     version: 0.8.23 (venv) vs 0.8.23 (this install)\n"
    )


class _Result:
    def __init__(self, stdout: str, rc: int = 0):
        self.stdout = stdout
        self.stderr = ""
        self.returncode = rc


class TestProbeTimeout(unittest.TestCase):
    """The defect: a timeout must not masquerade as 'not installed'."""

    def test_timeout_is_unknown_not_inactive(self):
        exc = subprocess.TimeoutExpired(cmd=" ".join(PROBE_CMD), timeout=60)
        with patch("toolrecall.healthcheck.subprocess.run", side_effect=exc):
            frag = hc._probe_shim_state()
        self.assertEqual(frag["shim_state"], "unknown")
        self.assertEqual(frag["shim_reason"], "probe_timeout")
        self.assertIsNotNone(frag["shim_probe_s"])
        self.assertGreaterEqual(frag["shim_probe_s"], 0.0)

    def test_timeout_note_says_timed_out_and_never_says_install(self):
        note = hc.warnings(
            _mk_info(shim_state="unknown", shim_reason="probe_timeout", shim_probe_s=61.4)
        )
        joined = "; ".join(note).lower()
        self.assertIn("timed out", joined)
        self.assertNotIn("install", joined)
        self.assertIn("61.4", "; ".join(note))

    def test_spawn_error_is_unknown_and_names_the_reason(self):
        with patch(
            "toolrecall.healthcheck.subprocess.run", side_effect=FileNotFoundError("toolrecall")
        ):
            frag = hc._probe_shim_state()
        self.assertEqual(frag["shim_state"], "unknown")
        self.assertEqual(frag["shim_reason"], "probe_error")

    def test_probe_budget_is_not_a_coin_flip(self):
        # Measured cost of one probe on Robin's box: 3.2 s warm, 10.8-17.1 s
        # loaded, against a 20 s budget. A daily job can afford a wide margin.
        self.assertGreaterEqual(hc.SHIM_PROBE_TIMEOUT_S, 60.0)

    def test_the_configured_budget_is_actually_passed_to_subprocess(self):
        calls = []

        def _fake(cmd, **kw):
            calls.append((cmd, kw))
            return _Result(_probe_out(True, True))

        with patch("toolrecall.healthcheck.subprocess.run", side_effect=_fake):
            hc._probe_shim_state()
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1]["timeout"], hc.SHIM_PROBE_TIMEOUT_S)


class TestSuccessPredicate(unittest.TestCase):
    """One contract for both consumers: applied: yes AND probe: pass."""

    def _frag(self, applied: bool, passing: bool) -> dict:
        with patch(
            "toolrecall.healthcheck.subprocess.run",
            side_effect=lambda *a, **kw: _Result(_probe_out(applied, passing)),
        ):
            return hc._probe_shim_state()

    def test_applied_and_probe_pass_is_active(self):
        frag = self._frag(True, True)
        self.assertEqual(frag["shim_state"], "active")
        self.assertEqual(frag["shim_reason"], "")

    def test_applied_without_probe_pass_is_inactive_probe_failed(self):
        frag = self._frag(True, False)
        self.assertEqual(frag["shim_state"], "inactive")
        self.assertEqual(frag["shim_reason"], "probe_failed")

    def test_probe_pass_without_applied_is_inactive_applied_missing(self):
        # A probe-passing .pth that is not applied means every real interpreter
        # still starts unshimmed (the 2026-11-07 zz_ ordering bug).
        frag = self._frag(False, True)
        self.assertEqual(frag["shim_state"], "inactive")
        self.assertEqual(frag["shim_reason"], "applied_missing")

    def test_no_evidence_is_inactive_without_reason(self):
        frag = self._frag(False, False)
        self.assertEqual(frag["shim_state"], "inactive")
        self.assertEqual(frag["shim_reason"], "")


class TestGivenUpShimIsNotReportedAsMissing(unittest.TestCase):
    """An inactive shim still deserves the install hint — unknown must not."""

    def test_inactive_keeps_install_hint_available(self):
        note = "; ".join(hc.warnings(_mk_info(shim_state="inactive", shim_reason=""))).lower()
        self.assertNotIn("timed out", note)

    def test_render_shows_the_state(self):
        line = hc.render(_mk_info(shim_state="unknown", shim_reason="probe_timeout"))
        self.assertIn("shim=unknown", line)


if __name__ == "__main__":
    unittest.main()
