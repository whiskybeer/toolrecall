# Copyright (c) 2026 Robin Schultka
# SPDX-License-Identifier: MIT
# Source: https://github.com/whiskybeer/toolrecall
"""Tests: toolrecall.stub_core — the shared unchanged-file stub state machine.

One contract for both surfaces (bridge + shim). These tests pin the state
machine itself; surface tests (test_shim_stub.py, test_mcp_bridge.py) pin
the policy + wiring.
"""

import os
import tempfile
import unittest

from toolrecall.stub_core import FileStubSession, note_agent, note_mcp


class StubSessionTestCase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.counted: list[tuple[int, str, str]] = []

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)

    BODY = "x = 1\n" + "# pad " + "y" * 4000 + "\n"  # >800 chars

    def _path(self, name="probe.py", body=None):
        p = os.path.join(self.tmpdir, name)
        with open(p, "w") as f:
            f.write(body or self.BODY)
        return p

    def _session(self, **kw):
        kw.setdefault("counter_fn", lambda t, p, n="": self.counted.append((t, p, n)))
        return FileStubSession(**kw)


class TestFileStubSession(StubSessionTestCase):
    def test_first_read_registers_second_stubs(self):
        s = self._session()
        p = self._path()
        self.assertIsNone(s.maybe_stub(p, self.BODY))
        note = s.maybe_stub(p, self.BODY)
        self.assertIsNotNone(note)
        self.assertIn("unchanged", note.lower())

    def test_consecutive_armor_sends_full_on_third(self):
        s = self._session(max_consecutive=2)
        p = self._path()
        s.maybe_stub(p, self.BODY)  # register
        self.assertIsNotNone(s.maybe_stub(p, self.BODY))  # stub 1
        self.assertIsNotNone(s.maybe_stub(p, self.BODY))  # stub 2
        # 3rd consecutive read: full content (compaction armor)
        self.assertIsNone(s.maybe_stub(p, self.BODY))

    def test_default_consecutive_armor_is_five(self):
        s = self._session(min_chars=0)
        p = self._path()
        s.maybe_stub(p, self.BODY)  # register
        for _ in range(5):
            self.assertIsNotNone(s.maybe_stub(p, self.BODY))  # stubs 1..5
        # 6th read: full content (compaction armor)
        self.assertIsNone(s.maybe_stub(p, self.BODY))

    def test_cap_is_configurable_bridge_policy_three(self):
        s = self._session(max_consecutive=3, min_chars=0)
        p = self._path()
        s.maybe_stub(p, self.BODY)
        self.assertIsNotNone(s.maybe_stub(p, self.BODY))
        self.assertIsNotNone(s.maybe_stub(p, self.BODY))
        # cap 3 → third consecutive read still stubs, 4th is full
        self.assertIsNotNone(s.maybe_stub(p, self.BODY))
        self.assertIsNone(s.maybe_stub(p, self.BODY))

    def test_changed_digest_reregisters(self):
        s = self._session()
        p = self._path()
        s.maybe_stub(p, self.BODY)
        self.assertIsNone(s.maybe_stub(p, "BODY2"))

    def test_non_agent_never_stubs(self):
        s = self._session()
        p = self._path()
        s.maybe_stub(p, self.BODY)
        self.assertIsNone(s.maybe_stub(p, self.BODY, is_agent=False))

    def test_lossy_content_never_stubs(self):
        s = self._session()
        p = self._path()
        s.maybe_stub(p, self.BODY)
        self.assertIsNone(s.maybe_stub(p, self.BODY, lossless=False))

    def test_small_content_never_stubs(self):
        s = self._session()
        p = self._path()
        s.maybe_stub(p, "tiny\n")
        self.assertIsNone(s.maybe_stub(p, "tiny\n"))

    def test_floor_zero_stubs_any_size(self):
        s = self._session(min_chars=0)
        p = self._path()
        s.maybe_stub(p, "tiny\n")
        self.assertIsNotNone(s.maybe_stub(p, "tiny\n"))

    def test_ttl_zero_disables_stubbing_entirely(self):
        s = self._session(ttl_s=0)
        p = self._path()
        self.assertIsNone(s.maybe_stub(p, self.BODY))
        self.assertIsNone(s.maybe_stub(p, self.BODY))

    def test_ttl_expiry_sends_full_and_restarts_streak(self):
        s = self._session(ttl_s=100)
        p = self._path()
        s.maybe_stub(p, self.BODY)
        self.assertIsNotNone(s.maybe_stub(p, self.BODY))
        # age the last full send beyond the TTL
        key = os.path.realpath(p)
        s._session[key]["full_sent_at"] -= s.ttl_s + 1
        # expiry → full send; the streak restarts (streak predating the
        # gap says nothing about what the agent holds now)
        self.assertIsNone(s.maybe_stub(p, self.BODY))
        self.assertEqual(s._session[key]["consecutive"], 0)
        self.assertIsNotNone(s.maybe_stub(p, self.BODY))

    def test_reset_path_clears_state(self):
        s = self._session()
        p = self._path()
        s.maybe_stub(p, self.BODY)
        s.reset_path(p)
        # fresh register (full send), not a stub
        self.assertIsNone(s.maybe_stub(p, self.BODY))

    def test_reset_path_normalizes_path(self):
        s = self._session()
        p = self._path("sub dir file.py")
        s.maybe_stub(p, self.BODY)
        s.reset_path(os.path.join(self.tmpdir, ".", "sub dir file.py"))
        self.assertIsNone(s.maybe_stub(p, self.BODY))

    def test_empty_path_is_safe(self):
        s = self._session()
        self.assertIsNone(s.maybe_stub("", self.BODY))
        s.reset_path("")
        s.reset_all()  # must not raise

    def test_counter_receives_tokens_path_note(self):
        s = self._session()
        p = self._path()
        s.maybe_stub(p, self.BODY)
        s.maybe_stub(p, self.BODY)  # the stub
        self.assertEqual(len(self.counted), 1)
        tokens, cpath, note = self.counted[0]
        self.assertEqual(tokens, len(self.BODY) // 4)
        self.assertEqual(os.path.realpath(cpath), os.path.realpath(p))
        self.assertTrue(note)  # note rides to the counter (access-log detail)

    def test_counter_only_fires_on_stubs(self):
        s = self._session()
        p = self._path()
        s.maybe_stub(p, self.BODY)  # register — no count
        s.maybe_stub(p, self.BODY)  # stub — count
        s.maybe_stub(p, "changed")  # digest change — no count
        self.assertEqual(len(self.counted), 1)

    def test_counter_failure_never_breaks_read(self):
        def boom(tokens, path, note=""):
            raise OSError("daemon down")

        s = self._session(counter_fn=boom)
        p = self._path()
        s.maybe_stub(p, self.BODY)
        note = s.maybe_stub(p, self.BODY)  # must not raise
        self.assertIsNotNone(note)

    def test_note_fns_are_surface_specific(self):
        self.assertIn("bypass_cache=true", note_mcp(0))
        self.assertNotIn("bypass_cache=true", note_agent(0))
        self.assertIn("cache_refresh_file", note_agent(0))


if __name__ == "__main__":
    unittest.main()
