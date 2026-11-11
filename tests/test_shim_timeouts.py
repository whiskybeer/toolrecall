"""Shim timeout counter semantics.

The counter is what makes the breaker visible in `toolrecall stats`, so its
numbers must mean what they say: `total` is the lifetime count across the file
that is live AND the rotated one, while `last_24h` filters by record time no
matter which file a record sits in.
"""

import json
import os
import shutil
import tempfile
import time
import unittest
from unittest import mock

from toolrecall import shim_timeouts as st


class SummaryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="tr_st_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.live = os.path.join(self.tmp, "shim_timeouts.jsonl")
        self.rotated = self.live + ".1"
        patcher = mock.patch.object(st, "_log_path", lambda: self.live)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _write(self, path: str, n: int, ts: float | None = None) -> None:
        with open(path, "a", encoding="utf-8") as f:
            for _ in range(n):
                f.write(
                    json.dumps({"ts": ts or time.time(), "path": "/x", "reason": "timeout"}) + "\n"
                )

    def test_total_includes_the_rotated_file(self):
        self._write(self.rotated, 4)
        self._write(self.live, 2)
        summary = st.summary()
        self.assertEqual(summary["total"], 6)
        self.assertEqual(summary["last_24h"], 6)

    def test_missing_files_are_zero_not_an_error(self):
        self.assertEqual(st.summary(), {"total": 0, "last_24h": 0, "last": None})

    def test_last_24h_ignores_old_records_in_either_file(self):
        self._write(self.rotated, 3, ts=time.time() - 3 * 86400)
        self._write(self.live, 1)
        summary = st.summary()
        self.assertEqual(summary["total"], 4)
        self.assertEqual(summary["last_24h"], 1)

    def test_truncated_last_line_is_tolerated(self):
        self._write(self.live, 1)
        with open(self.live, "a", encoding="utf-8") as f:
            f.write('{"ts": 1, "path"')  # writer died mid-line
        self.assertEqual(st.summary()["total"], 1)

    def test_record_survives_an_unwritable_location(self):
        bad = mock.patch.object(st, "_log_path", lambda: os.path.join(self.tmp, "nope", "\0bad"))
        with bad:
            st.record("/x")  # must not raise


if __name__ == "__main__":
    unittest.main()
