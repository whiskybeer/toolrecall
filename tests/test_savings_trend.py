# Copyright (c) 2026 Robin Schultka
# SPDX-License-Identifier: MIT
# Source: https://github.com/whiskybeer/toolrecall
"""Savings trend (usage CSV) + infra exclusion in the I/O trend.

Why this module exists (measured 2026-09-22): `toolrecall trend` showed
only two days, and only disk I/O. Both had structural causes:

  * access_log is a rolling 50,000-row window. Gateway polling of its own
    config, cron jobs and plugin manifests held 47,931 of those rows, so
    the user's real history was evicted within ~2 days.
  * daily_trend() queries category='file_cache' and counts rows, never
    tokens — it is a disk hit-rate series by construction, and the LLM
    saving (mcp_dedup) had 7 rows in the whole window.

The append-only usage CSV already held 49 days of per-request truth, so
the savings series reads from there instead.

Hermetic: temp CSV + temp DB, never the production files.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone

from toolrecall import trend

_HEADER = (
    "timestamp,cache_status,target_host,target_path,request_hash,"
    "prompt_tokens,completion_tokens,cache_read_tokens,cache_write_tokens,"
    "cost_usd,dedup_tokens,cache_discount_usd\n"
)


def _ts(days_ago: int, hour: int = 12) -> float:
    d = datetime.now(timezone.utc).replace(
        hour=hour, minute=0, second=0, microsecond=0
    ) - timedelta(days=days_ago)
    return d.timestamp()


def _write(path: str, rows: list[tuple]) -> None:
    """rows: (ts, status, path, prompt, dedup, cost)"""
    with open(path, "w") as f:
        f.write(_HEADER)
        for ts, status, target, prompt, dedup, cost in rows:
            f.write(f"{ts},{status},openrouter.ai,{target},h,{prompt},10,0,0,{cost},{dedup},0.0\n")


class TestSavingsTrend(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.csv = os.path.join(self.tmp.name, "usage.csv")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_groups_per_day_oldest_first(self) -> None:
        _write(
            self.csv,
            [
                (_ts(2), "STREAM", "/v1/chat/completions", 10_000, 500, 0.10),
                (_ts(1), "STREAM", "/v1/chat/completions", 20_000, 0, 0.20),
                (_ts(0), "STREAM", "/v1/chat/completions", 30_000, 100, 0.30),
            ],
        )
        rows = trend.savings_trend(days=None, usage_csv=self.csv)
        self.assertEqual(len(rows), 3)
        self.assertLess(rows[0]["date"], rows[-1]["date"])
        self.assertEqual(rows[0]["sent"], 10_000)
        self.assertEqual(rows[0]["not_sent"], 500)

    def test_cached_calls_count_as_not_sent_not_as_sent(self) -> None:
        """A HIT is a call the model never saw — the strongest saving."""
        _write(
            self.csv,
            [
                (_ts(0), "STREAM", "/v1/chat/completions", 50_000, 0, 0.50),
                (_ts(0), "HIT", "/v1/chat/completions", 40_000, 0, 0.40),
            ],
        )
        row = trend.savings_trend(days=None, usage_csv=self.csv)[0]
        self.assertEqual(row["sent"], 50_000)
        self.assertEqual(row["replay_tokens"], 40_000)
        self.assertEqual(row["replay_calls"], 1)
        self.assertEqual(row["not_sent"], 40_000)
        # Replayed price is not a new charge.
        self.assertAlmostEqual(row["cost_usd"], 0.50, places=6)

    def test_saved_pct_uses_sent_plus_not_sent_as_base(self) -> None:
        _write(
            self.csv,
            [
                (_ts(0), "STREAM", "/v1/chat/completions", 75_000, 25_000, 1.0),
            ],
        )
        row = trend.savings_trend(days=None, usage_csv=self.csv)[0]
        self.assertEqual(row["saved_pct"], 25.0)

    def test_metadata_endpoints_are_ignored(self) -> None:
        _write(
            self.csv,
            [
                (_ts(0), "HIT", "/api/v1/models", 0, 0, 0.0),
                (_ts(0), "STREAM", "/v1/chat/completions", 1_000, 0, 0.01),
            ],
        )
        row = trend.savings_trend(days=None, usage_csv=self.csv)[0]
        self.assertEqual(row["calls"], 1)

    def test_anthropic_messages_path_is_counted(self) -> None:
        _write(self.csv, [(_ts(0), "MISS", "/v1/messages", 7_000, 0, 0.07)])
        rows = trend.savings_trend(days=None, usage_csv=self.csv)
        self.assertEqual(rows[0]["sent"], 7_000)

    def test_days_limit_keeps_the_most_recent(self) -> None:
        _write(
            self.csv,
            [
                (_ts(5), "STREAM", "/v1/chat/completions", 1, 0, 0.0),
                (_ts(4), "STREAM", "/v1/chat/completions", 2, 0, 0.0),
                (_ts(0), "STREAM", "/v1/chat/completions", 3, 0, 0.0),
            ],
        )
        rows = trend.savings_trend(days=2, usage_csv=self.csv)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[-1]["sent"], 3)

    def test_missing_csv_is_empty_not_error(self) -> None:
        self.assertEqual(trend.savings_trend(usage_csv=os.path.join(self.tmp.name, "nope.csv")), [])


class TestInfraExclusion(unittest.TestCase):
    """Agent plumbing must not evict the user's history from the I/O trend."""

    def test_predicate_excludes_plumbing_keeps_work(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db = os.path.join(tmp.name, "t.db")
        con = sqlite3.connect(db)
        con.execute(
            "CREATE TABLE access_log (id INTEGER PRIMARY KEY, category TEXT, "
            "path TEXT, hit INT, tokens INT, cached_at REAL, client TEXT)"
        )
        now = time.time()
        rows = [
            ("file_cache", "/home/u/.hermes/config.yaml", 1, 5, now, ""),
            ("file_cache", "/home/u/.hermes/cron/jobs.json", 1, 5, now, ""),
            ("file_cache", "/home/u/.hermes/plugins/x/plugin.yaml", 1, 5, now, ""),
            ("file_cache", "/home/u/.toolrecall/cache.db", 1, 5, now, ""),
            ("file_cache", "/home/u/project/main.py", 1, 5, now, ""),
            # A user's own YAML inside their project is real work, not infra.
            ("file_cache", "/home/u/project/config.yaml", 0, 5, now, ""),
        ]
        con.executemany(
            "INSERT INTO access_log (category,path,hit,tokens,cached_at,client) "
            "VALUES (?,?,?,?,?,?)",
            rows,
        )
        con.commit()
        kept = [
            r[0]
            for r in con.execute(f"SELECT path FROM access_log WHERE {trend._infra_filter_sql()}")
        ]
        con.close()
        self.assertEqual(sorted(kept), ["/home/u/project/config.yaml", "/home/u/project/main.py"])


if __name__ == "__main__":
    unittest.main()
