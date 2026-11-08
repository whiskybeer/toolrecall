"""Test: dedup_tokens column in proxy_usage.csv (the sent-X / not-sent-Y row).

The v3 header adds `dedup_tokens` — wire-verified tokens removed from a
request by request-body dedup before billing. Rows pre-v3 have no column;
DictReader returns None → .get() or 0 (same back-compat contract as
cost_usd).
"""

import os
import unittest


class TestDedupTokensColumn(unittest.TestCase):
    def setUp(self):
        import tempfile

        self.tmpdir = tempfile.mkdtemp()
        self._orig = os.environ.get("TOOLRECALL_USAGE_LOG")  # not used; path via module attr
        import toolrecall.proxy as proxy

        self.proxy = proxy
        self.csv_path = os.path.join(self.tmpdir, "usage.csv")
        self._orig_path = proxy._USAGE_LOG_PATH
        proxy._USAGE_LOG_PATH = self.csv_path

    def tearDown(self):
        self.proxy._USAGE_LOG_PATH = self._orig_path

    def test_row_carries_dedup_tokens(self):
        self.proxy._init_usage_log()
        self.proxy._log_proxy_usage(
            "STREAM", "api.openai.com", "/v1/chat/completions", "abc123", dedup_tokens=1234
        )
        import csv as _csv

        with open(self.csv_path) as f:
            row = next(iter(_csv.DictReader(f)))
        self.assertEqual(row["dedup_tokens"], "1234")
        self.assertEqual(row["cache_status"], "STREAM")

    def test_default_zero_when_no_dedup(self):
        self.proxy._init_usage_log()
        self.proxy._log_proxy_usage("STREAM", "h", "/p", "abc")
        import csv as _csv

        with open(self.csv_path) as f:
            row = next(iter(_csv.DictReader(f)))
        self.assertEqual(row["dedup_tokens"], "0")

    def test_money_today_sums_dedup_and_ignores_old_rows(self):
        self.proxy._init_usage_log()
        import time as _t

        now = _t.time()
        self.proxy._log_proxy_usage("STREAM", "h", "/p", "a1", dedup_tokens=500)
        self.proxy._log_proxy_usage("STREAM", "h", "/p", "a2", dedup_tokens=700)
        # v2-era row without the column
        with open(self.csv_path, "a") as f:
            f.write(f"{now:.3f},STREAM,h,/p,old,1000,10,0,0,0\n")
        from toolrecall.tui_top import money_today

        m = money_today(self.csv_path)
        self.assertEqual(m["requests"], 3)
        self.assertEqual(m["dedup_tokens"], 1200)


if __name__ == "__main__":
    unittest.main()
