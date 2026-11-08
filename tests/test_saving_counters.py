# Copyright (c) 2026 Robin Schultka
# SPDX-License-Identifier: MIT
# Source: https://github.com/whiskybeer/toolrecall
"""Regression tests for two mis-signed saving counters (found 2026-09-21).

Bug 1 — `money_today` counted HIT rows as SENT. A HIT is a call ToolRecall
answered entirely from cache: the model was never asked, so its prompt
tokens are the strongest saving there is. Summing them into `prompt` put
that saving on the cost side of the ledger (7,289 lifetime HITs, 39.1M
tokens). Cost on a HIT row is the cached body's replayed price, not money
charged again, so it must not inflate billed truth either.

Bug 2 — `cached_api_check` computed `io_tokens_saved` and dropped it on the
floor: `_record("api_cache", True)` was called without it, so api_cache
showed hits with io_tokens_saved=0 forever.

Hermetic: temp CSV / temp DB only. The production CSV
(~/.toolrecall/proxy_usage.csv) is billed truth and is never read here.
"""

from __future__ import annotations

import os
import tempfile
import time
import unittest

from toolrecall import tui_top

_HEADER = (
    "timestamp,cache_status,target_host,target_path,request_hash,"
    "prompt_tokens,completion_tokens,cache_read_tokens,cache_write_tokens,"
    "cost_usd,dedup_tokens,cache_discount_usd\n"
)


def _write(path: str, rows: list[tuple]) -> None:
    """rows: (ts, status, prompt, completion, cost)"""
    with open(path, "w") as f:
        f.write(_HEADER)
        for ts, status, prompt, completion, cost in rows:
            f.write(
                f"{ts},{status},openrouter.ai,/v1/chat/completions,h,"
                f"{prompt},{completion},0,0,{cost},0,0.0\n"
            )


class TestHitRowsAreNotCountedAsSent(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.csv = os.path.join(self.tmp.name, "usage.csv")
        self.now = time.time()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_hit_prompt_tokens_are_not_in_sent(self) -> None:
        _write(
            self.csv,
            [
                (self.now, "STREAM", 100_000, 500, 0.50),
                (self.now, "HIT", 80_000, 400, 0.40),
            ],
        )
        m = tui_top.money_today(usage_csv=self.csv)
        assert m is not None
        # Only the real call counts as sent.
        self.assertEqual(m["prompt"], 100_000)
        # The cached call's tokens are reported separately, as a saving.
        self.assertEqual(m["replay_tokens"], 80_000)
        self.assertEqual(m["replays"], 1)

    def test_hit_cost_does_not_inflate_billed_truth(self) -> None:
        """A replayed price is not a charge — the provider was never called."""
        _write(
            self.csv,
            [
                (self.now, "MISS", 10_000, 100, 0.25),
                (self.now, "HIT", 10_000, 100, 0.25),
            ],
        )
        m = tui_top.money_today(usage_csv=self.csv)
        assert m is not None
        self.assertAlmostEqual(m["cost_usd"], 0.25, places=6)

    def test_hit_completion_tokens_are_not_billed(self) -> None:
        _write(self.csv, [(self.now, "HIT", 5_000, 700, 0.0)])
        m = tui_top.money_today(usage_csv=self.csv)
        assert m is not None
        self.assertEqual(m["completion"], 0)
        self.assertEqual(m["prompt"], 0)

    def test_hit_sizes_excluded_from_median(self) -> None:
        """The median is a diagnostic of what the model actually receives."""
        _write(
            self.csv,
            [
                (self.now, "STREAM", 50_000, 10, 0.1),
                (self.now, "HIT", 900_000, 10, 0.0),
            ],
        )
        m = tui_top.money_today(usage_csv=self.csv)
        assert m is not None
        self.assertEqual(m["median"], 50_000)

    def test_requests_still_counts_every_row(self) -> None:
        _write(
            self.csv,
            [
                (self.now, "STREAM", 1_000, 10, 0.01),
                (self.now, "HIT", 1_000, 10, 0.0),
            ],
        )
        m = tui_top.money_today(usage_csv=self.csv)
        assert m is not None
        self.assertEqual(m["requests"], 2)


class TestApiCacheRecordsTokensSaved(unittest.TestCase):
    """api_cache hits must carry their saving into cache_stats."""

    def test_record_is_called_with_tokens_saved(self) -> None:
        import inspect

        from toolrecall import cache as cache_mod

        src = inspect.getsource(cache_mod.cached_api_check)
        # Both SWR tiers (fresh + serve-stale) must pass the computed value.
        self.assertEqual(
            src.count('_record("api_cache", True, io_tokens_saved=io_tokens_saved)'), 2
        )
        self.assertNotIn('_record("api_cache", True)\n', src)


if __name__ == "__main__":
    unittest.main()
