"""Session-replay tier: naive-world reconstruction from wire data.

The umbrella counts removed tokens once per event; the replay reconstructs
what the naive world (no ToolRecall) would have billed for the SAME
sessions: every request's naive size = billed prompt + in-request dedup +
all stub tokens stubbed earlier in the session (history rides along in a
naive prompt). Sessions split at >15 min request gaps; stubs attach to the
next request within the same gap rule; HIT rows excluded (no request
prompt). Wall crossings use tui_top._CONTEXT_WALL.

All fixtures here are synthetic and deterministic; the prod CSV/DB are
never touched (usage_csv/db passed explicitly).
"""

import os
import sqlite3
import tempfile
import time
import unittest

from toolrecall import tui_top

_HDR = (
    "timestamp,cache_status,target_host,target_path,request_hash,"
    "prompt_tokens,completion_tokens,cache_read_tokens,cache_write_tokens,cost_usd,"
    "dedup_tokens,cache_discount_usd\n"
)


def _csv_row(ts, status, p, d):
    return f"{ts:.3f},{status},h,/p,hash{ts:.0f},{p},10,0,0,0.01,{d},0.0\n"


class TestSessionReplay(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.csv = os.path.join(self.tmp, "usage.csv")
        self.db = os.path.join(self.tmp, "cache.db")
        con = sqlite3.connect(self.db)
        con.execute(
            "CREATE TABLE access_log (category TEXT NOT NULL, path TEXT,"
            " hit INTEGER NOT NULL, tokens INTEGER DEFAULT 0, cached_at REAL NOT NULL)"
        )
        con.commit()
        con.close()

    def _seed_csv(self, rows):
        with open(self.csv, "w") as f:
            f.write(_HDR)
            for r in rows:
                f.write(r)

    def _seed_stub(self, ts, tokens):
        con = sqlite3.connect(self.db)
        con.execute(
            "INSERT INTO access_log (category, path, hit, tokens, cached_at)"
            " VALUES ('mcp_dedup', '/tmp/f', 0, ?, ?)",
            (tokens, ts),
        )
        con.commit()
        con.close()

    def test_two_sessions_replay_arithmetic(self):
        day = time.time() - 3600  # safely inside today
        t1, t2, t3 = day + 1000, day + 1060, day + 1120
        t4 = day + 8000  # 2h gap → new session
        self._seed_csv(
            [
                _csv_row(t1, "STREAM", 10_000, 0),
                _csv_row(t2, "STREAM", 15_000, 2_000),
                _csv_row(t3, "STREAM", 20_000, 0),
                _csv_row(t4, "STREAM", 30_000, 0),
            ]
        )
        self._seed_stub(t2 + 10, 5_000)  # between r2 and r3, same session
        self._seed_stub(t4 + 100, 7_000)  # no later request → dropped

        rp = tui_top.session_replay_today(usage_csv=self.csv, db=self.db)
        self.assertIsNotNone(rp)
        self.assertEqual(rp["requests"], 4)
        self.assertEqual(rp["sessions"], 2)
        self.assertEqual(rp["stubs_attached"], 1)
        self.assertEqual(rp["stubs_dropped"], 1)
        # Session 1: r1 n=10_000; r2 n=15_000+2_000=17_000; r3 n=20_000+5_000=25_000
        # Session 2: r4 n=30_000 (stub after last request never rides along)
        self.assertEqual(rp["actual_total"], 75_000)
        self.assertEqual(rp["naive_total"], 82_000)
        self.assertAlmostEqual(rp["savings_pct"], 100 * (1 - 75 / 82), places=6)
        self.assertAlmostEqual(rp["amp"], 82 / 75, places=6)
        self.assertEqual(rp["peak_actual"], 30_000)
        self.assertEqual(rp["peak_naive"], 30_000)
        # Real 200K wall: nothing over.
        self.assertEqual(rp["wall_naive"], 0)
        self.assertEqual(rp["wall_act"], 0)

    def test_hit_rows_excluded_from_request_stream(self):
        day = time.time() - 3600
        self._seed_csv(
            [
                _csv_row(day + 100, "STREAM", 10_000, 0),
                _csv_row(day + 200, "HIT", 999_999, 0),  # replay row — no request prompt
            ]
        )
        rp = tui_top.session_replay_today(usage_csv=self.csv, db=self.db)
        self.assertEqual(rp["requests"], 1)
        self.assertEqual(rp["actual_total"], 10_000)
        self.assertEqual(rp["naive_total"], 10_000)
        self.assertEqual(rp["sessions"], 1)

    def test_wall_counts_naive_overflow(self):
        day = time.time() - 3600
        self._seed_csv(
            [
                _csv_row(day + 100, "STREAM", 10_000, 0),
                _csv_row(day + 160, "STREAM", 15_000, 0),
                _csv_row(day + 220, "STREAM", 19_000, 0),
            ]
        )
        self._seed_stub(day + 150, 5_000)  # rides along in r2 (n=20_000) and r3 (n=24_000)
        orig = tui_top._CONTEXT_WALL
        try:
            tui_top._CONTEXT_WALL = 21_000
            rp = tui_top.session_replay_today(usage_csv=self.csv, db=self.db)
        finally:
            tui_top._CONTEXT_WALL = orig
        # naive: r2=20_000 ok, r3=24_000 over → 1; actual: max 19_000 → 0
        self.assertEqual(rp["wall_naive"], 1)
        self.assertEqual(rp["wall_act"], 0)
        self.assertEqual(rp["naive_total"], 54_000)
        self.assertEqual(rp["actual_total"], 44_000)

    def test_no_traffic_returns_none(self):
        self.assertIsNone(tui_top.session_replay_today(usage_csv=self.csv + ".missing", db=self.db))
        self._seed_csv([])  # header only
        self.assertIsNone(tui_top.session_replay_today(usage_csv=self.csv, db=self.db))

    def test_render_plain_shows_replay_tier(self):
        orig_money = tui_top.money_today
        try:
            tui_top.money_today = lambda usage_csv=None: None  # no billed block
            snap = {
                "stats": {"_daemon": False},
                "db": "/dev/null",
                "db_path": "/dev/null",
                # render_plain consumes the dict key (pure renderer — the
                # collector is never called here).
                "replay": {
                    "requests": 4,
                    "sessions": 2,
                    "stubs_attached": 1,
                    "stubs_dropped": 1,
                    "actual_total": 75_000,
                    "naive_total": 300_000,
                    "avoided_total": 225_000,
                    "dedup_sum": 25_000,
                    "stub_attached_tokens": 30_000,
                    "amp_stubs": 6.5,
                    "ride_slots": 2,
                    "savings_pct": 75.0,
                    "amp": 4.0,
                    "peak_actual": 30_000,
                    "peak_naive": 210_000,
                    "wall_naive": 5,
                    "wall_act": 1,
                    "limit": 200_000,
                },
            }
            out = tui_top.render_plain(snap)
            # Headline is the not-sent number; the comparison follows.
            self.assertIn("SAVED TO LLM TODAY:  225.0K tok", out)
            # peak_naive (210K) > peak_actual (30K) → biggest-message line.
            self.assertIn("Biggest single message:  30.0K tok", out)
            self.assertIn("without ToolRecall 210.0K tok", out)
            self.assertIn("same removed content piled up again", out)
            self.assertIn("5 of 4 messages would not have fit at all", out)
        finally:
            tui_top.money_today = orig_money


if __name__ == "__main__":
    unittest.main()
