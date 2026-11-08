# Copyright (c) 2026 Robin Schultka
# SPDX-License-Identifier: MIT
# Source: https://github.com/whiskybeer/toolrecall
"""Tests: native-write tracker blindness fix (context_tracker externals).

``mark_dirty`` only fires for writes through the daemon
(cached_write/cached_patch). An agent writing via its own native tools
(``open(path, 'w')`` in-process) used to be invisible: the file stayed in
the read set with its read-time mtime, and get_dirty/get_stale kept
calling it "clean". The fix re-stats read-set mtimes at query time
(_externally_modified) — the same re-validation restore() has always done
at daemon boot — and surfaces out-of-band writes as
``externally_modified`` / stale entries with ``external: True``.

Covers: detection + both response shapes, daemon-write non-duplication,
re-read clearing, deletion exclusion, sensitive-path exclusion, purity
under polling, externals_detail content.
"""

import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from toolrecall.context_tracker import ContextTracker


def _make_file(tmpdir: str, name: str, content: str = "hello") -> str:
    p = os.path.join(tmpdir, name)
    with open(p, "w") as f:
        f.write(content)
    return p


class TestExternallyModified(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.tr = ContextTracker()

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _read(self, path: str) -> None:
        self.tr.mark_read(path)

    def _write_natively(self, path: str) -> None:
        # The whole point: NOT mark_dirty — a real open('w') like the
        # agent's native write tool would do (shim path, daemon-blind).
        with open(path, "w") as f:
            f.write("rewritten by the agent's native write tool")

    def test_native_write_surfaces_in_get_dirty(self):
        p = _make_file(self.tmpdir, "a.py")
        self._read(p)
        time.sleep(0.02)  # mtime granularity
        self._write_natively(p)
        res = self.tr.get_dirty()
        self.assertIn(p, res["externally_modified"])
        self.assertEqual(res["total_externally_modified"], 1)
        self.assertIn(p, res["clean"])  # legacy field unchanged (contract)

    def test_native_write_surfaces_in_get_stale(self):
        p = _make_file(self.tmpdir, "b.py")
        self._read(p)
        time.sleep(0.02)
        self._write_natively(p)
        res = self.tr.get_stale()
        self.assertIn(p, res["paths"])
        entry = next(e for e in res["stale"] if e["path"] == p)
        self.assertTrue(entry["external"])
        self.assertEqual(entry["write_seq"], 0)
        self.assertGreater(entry["est_tokens"], 0)
        self.assertIn(p, res["externally_modified"])

    def test_daemon_write_not_double_reported(self):
        # mark_dirty (daemon write) after read → normal dirty path; the
        # externals list must NOT duplicate it.
        p = _make_file(self.tmpdir, "c.py")
        self._read(p)
        time.sleep(0.02)
        self.tr.mark_dirty(p)
        res = self.tr.get_dirty()
        self.assertIn(p, res["dirty"])
        self.assertNotIn(p, res["externally_modified"])
        stale = self.tr.get_stale()
        entry = next(e for e in stale["stale"] if e["path"] == p)
        self.assertFalse(entry.get("external", False))
        self.assertGreater(entry["write_seq"], 0)

    def test_untouched_read_stays_clean(self):
        p = _make_file(self.tmpdir, "d.py")
        self._read(p)
        time.sleep(0.02)  # ensure mtime is stable, not “about to change”
        res = self.tr.get_dirty()
        self.assertNotIn(p, res["externally_modified"])
        # Read, never written (by daemon or otherwise) → clean, as always.
        self.assertIn(p, res["clean"])
        self.assertEqual(self.tr.get_stale()["total_stale"], 0)

    def test_reread_clears_external_flag(self):
        p = _make_file(self.tmpdir, "e.py")
        self._read(p)
        time.sleep(0.02)
        self._write_natively(p)
        self.assertIn(p, self.tr.get_dirty()["externally_modified"])
        # Agent re-reads (sees current bytes) → mark_read refreshes mtime.
        self._read(p)
        self.assertNotIn(p, self.tr.get_dirty()["externally_modified"])
        self.assertEqual(self.tr.get_stale()["total_stale"], 0)

    def test_deleted_file_not_reported_as_external(self):
        # Deletion after a read is NOT a content change we can validate:
        # the context copy was correct at read time, a re-read errors
        # loudly on its own, and temp-file cleanup churn would make
        # deleted-file reports pure noise. Skipped — pinned here.
        # (get_stale's deleted-file branch covers only files the DAEMON
        # wrote before deletion — those have a write_seq to iterate.)
        p = _make_file(self.tmpdir, "f.py")
        self._read(p)
        time.sleep(0.02)
        os.remove(p)
        res = self.tr.get_dirty()
        self.assertNotIn(p, res["externally_modified"])
        self.assertEqual(self.tr.get_stale()["total_stale"], 0)

    def test_externals_detail_fields(self):
        p = _make_file(self.tmpdir, "g.py", content="z" * 800)
        self._read(p)
        time.sleep(0.02)
        self._write_natively(p)
        res = self.tr.get_dirty()
        self.assertEqual(len(res["externals_detail"]), 1)
        d = res["externals_detail"][0]
        self.assertEqual(d["path"], p)
        self.assertLess(d["read_mtime"], d["current_mtime"])
        self.assertEqual(d["est_tokens"], d["size"] // 4)

    def test_repeated_polls_are_stable(self):
        # Pure at query time: no state mutation, polling must not flip
        # results (this class of bug shipped once: the ctx_dropped
        # banked-once fix of 2026-09-10).
        p = _make_file(self.tmpdir, "h.py")
        self._read(p)
        time.sleep(0.02)
        self._write_natively(p)
        first = self.tr.get_dirty()["total_externally_modified"]
        for _ in range(5):
            self.assertEqual(self.tr.get_dirty()["total_externally_modified"], first)

    def test_sensitive_path_never_reported(self):
        # Defense in depth (same rule as get_stale): .env paths must not
        # appear in agent-visible externals even if a read ever tracked one.
        p = _make_file(self.tmpdir, ".env")
        self._read(p)
        time.sleep(0.02)
        self._write_natively(p)
        res = self.tr.get_dirty()
        self.assertNotIn(p, res["externally_modified"])
        self.assertNotIn(p, self.tr.get_stale()["externally_modified"])


if __name__ == "__main__":
    unittest.main()
