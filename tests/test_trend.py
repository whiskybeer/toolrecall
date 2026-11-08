"""Tests for toolrecall.trend — daily hit-rate trend from access_log.

Hermetic: every test points the DB at a fresh temp file via
TOOLRECALL_CACHE_DB and seeds access_log rows through the same SQL the
production cache layer writes. No daemon, no capsys (forbidden under
`make validate` — see tests/toolrecall-test-gates); stdout redirection is
done manually where the CLI entrypoint is exercised.
"""

import io
import json
import os
import sys
import tempfile
import time
from contextlib import redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import toolrecall.trend as trend_mod  # noqa: E402


class _TrendEnv:
    """Fresh temp DB per test; trend reads via a read-only connection.

    Deliberately does NOT go through cache._db() for seeding: the module
    singleton caches its Config at first use, so under a combined pytest
    run (another module importing toolrecall first) it would target the
    conftest hermetic.db instead of this test's env-pinned path. Direct
    sqlite3 on the same path toolrecall.trend resolves (fresh load_config)
    keeps the test order-independent.
    """

    def setup_method(self):
        self._tmp = tempfile.TemporaryDirectory()
        db_path = os.path.join(self._tmp.name, "trend-test.db")
        self._env_patcher = mock.patch.dict(os.environ, {"TOOLRECALL_CACHE_DB": db_path})
        self._env_patcher.start()
        self._db_path = db_path
        # Create schema exactly like production (cache.SCHEMA) so the
        # read-only connection in trend._connect_readonly finds the tables.
        import sqlite3

        from toolrecall._db import _init as _db_init
        from toolrecall.cache import SCHEMA

        conn = sqlite3.connect(db_path)
        conn.executescript(SCHEMA)
        conn.commit()
        conn.close()
        _db_init(SCHEMA)  # migrations / idempotent columns, same as prod

    def teardown_method(self):
        self._env_patcher.stop()
        self._tmp.cleanup()

    def _seed(
        self, day_epoch: float, path: str, hits: int, misses: int, category: str = "file_cache"
    ):
        """Write access_log rows the way cache._record does."""
        import sqlite3

        rows = []
        for hit in [1] * hits + [0] * misses:
            rows.append((category, path, hit, 0, day_epoch))
        conn = sqlite3.connect(self._db_path)
        conn.executemany(
            "INSERT INTO access_log (category, path, hit, tokens, cached_at)"
            " VALUES (?, ?, ?, ?, ?)",
            rows,
        )
        conn.commit()
        conn.close()

    def _day(self, offset_days: float) -> float:
        return time.time() - offset_days * 86400


class TestDailyTrendCategories(_TrendEnv):
    """The trend is per-category: file reads and MCP replays are never summed.

    MCP replays (synthetic `mcp:<server>/<tool>` paths) are I/O saving only —
    the response body still reaches the model — so mixing them into the
    file_cache series would inflate a number a user reads as real cache value.
    """

    def test_mcp_series_is_separate_from_file_series(self):
        d = self._day(0.1)
        self._seed(d, "/tmp/a.txt", hits=3, misses=1)
        self._seed(d, "mcp:hermes-docs/search_docs", hits=5, misses=0, category="mcp_cache")
        file_trend = trend_mod.daily_trend(7)
        mcp_trend = trend_mod.daily_trend(7, "mcp_cache")
        assert sum(t["reads"] for t in file_trend) == 4
        assert sum(t["reads"] for t in mcp_trend) == 5
        assert sum(t["hits"] for t in mcp_trend) == 5
        assert sum(t["hits"] for t in file_trend) == 3

    def test_default_category_excludes_mcp_rows(self):
        d = self._day(0.1)
        self._seed(d, "mcp:fetch/fetch_url", hits=2, misses=0, category="mcp_cache")
        assert trend_mod.daily_trend(7) == []

    def test_synthetic_mcp_path_survives_the_infra_filter(self):
        """`mcp:<server>/<tool>` must not be dropped as infrastructure noise."""
        d = self._day(0.1)
        self._seed(d, "mcp:sqlite-browser/read_query", hits=1, misses=1, category="mcp_cache")
        rows = trend_mod.daily_trend(7, "mcp_cache")
        assert len(rows) == 1
        assert rows[0]["reads"] == 2
        assert rows[0]["hit_pct"] == 50.0


class TestDailyTrend(_TrendEnv):
    def test_empty_log_yields_empty_trend(self):
        assert trend_mod.daily_trend() == []

    def test_two_days_aggregate_hits_and_misses(self):
        d0 = self._day(0.1)
        d1 = self._day(1.1)
        self._seed(d0, "/tmp/a.txt", hits=7, misses=3)
        self._seed(d1, "/tmp/b.txt", hits=1, misses=4)
        trend = trend_mod.daily_trend()
        assert len(trend) == 2
        by_date = {t["date"]: t for t in trend}
        today = sorted(by_date)[-1]
        yesterday = sorted(by_date)[0]
        assert by_date[today]["hits"] == 7 and by_date[today]["misses"] == 3
        assert by_date[today]["hit_pct"] == 70.0
        assert by_date[yesterday]["hits"] == 1 and by_date[yesterday]["misses"] == 4

    def test_days_limit_returns_most_recent(self):
        for i in range(4):
            self._seed(self._day(i + 0.1), "/tmp/x.txt", hits=i, misses=1)
        trend = trend_mod.daily_trend(days=2)
        assert len(trend) == 2
        assert trend[0]["date"] < trend[1]["date"]

    def test_only_file_cache_rows_counted(self):
        d0 = self._day(0.1)
        self._seed(d0, "/tmp/a.txt", hits=2, misses=1)
        from toolrecall import cache as cache_mod

        with cache_mod._db() as conn:
            conn.execute(
                "INSERT INTO access_log (category, path, hit, tokens, cached_at)"
                " VALUES ('terminal_cache', 'ls', 0, 0, ?)",
                (d0,),
            )
        trend = trend_mod.daily_trend()
        assert len(trend) == 1
        assert trend[0]["reads"] == 3


class TestMissClasses(_TrendEnv):
    def test_worst_class_first_with_share(self):
        base = self._day(0.05)
        # session transcripts: mostly misses, big share. NOT under ~/.hermes —
        # agent plumbing is excluded from the trend by design (it polls its own
        # config and would evict the user's history from the 50k window).
        for i in range(120):
            self._seed(base, f"/home/u/app/sessions/s{i}.json", hits=1, misses=4)
        # stable docs: mostly hits
        for i in range(150):
            self._seed(base, f"/home/u/docs/doc{i}.md", hits=9, misses=1)
        classes = trend_mod.miss_classes(min_reads=100)
        assert classes, "expected at least one class above min_reads"
        assert classes[0]["path_class"].endswith("sessions")
        assert classes[0]["hit_pct"] == 20.0
        doc_classes = [c for c in classes if c["path_class"].endswith("docs")]
        assert doc_classes and doc_classes[0]["hit_pct"] == 90.0

    def test_min_reads_filters_noise(self):
        base = self._day(0.05)
        for i in range(10):  # below the 100 default
            self._seed(base, f"/tmp/tiny{i}.txt", hits=0, misses=1)
        assert trend_mod.miss_classes() == []
        # Lower the threshold and the class appears.
        assert trend_mod.miss_classes(min_reads=5) != []

    def test_top_limits_output(self):
        base = self._day(0.05)
        for prefix in ("alpha", "beta", "gamma", "delta"):
            for i in range(120):
                self._seed(base, f"/data/{prefix}/f{i}.txt", hits=1, misses=2)
        classes = trend_mod.miss_classes(min_reads=100, top=2)
        assert len(classes) == 2


class TestReport(_TrendEnv):
    def test_human_report_contains_trend_and_hint(self):
        base = self._day(0.05)
        for i in range(120):
            self._seed(base, f"/home/u/app/sessions/s{i}.json", hits=1, misses=3)
        buf = io.StringIO()
        with redirect_stdout(buf):
            from toolrecall.trend import trend_report

            out = trend_report(days=7)
        buf.write(out)
        text = out
        # Savings lead the report; the I/O hit-rate table follows.
        assert "Tokens NOT sent to the model" in text
        assert "Disk reads served from cache" in text
        assert "hit%" in text
        assert "file_ttls" in text, "the actionable remedy hint must be present"

    def test_json_report_parses(self):
        base = self._day(0.05)
        self._seed(base, "/tmp/a.txt", hits=5, misses=2)
        from toolrecall.trend import trend_report

        data = json.loads(trend_report(days=7, as_json=True))
        assert {"savings_trend", "trend", "miss_classes", "coverage"} <= set(data)
        assert data["trend"][0]["hits"] == 5
        assert data["coverage"]["window_entries"] >= 7

    def test_empty_report_is_not_error(self):
        from toolrecall.trend import trend_report

        text = trend_report(days=7)
        assert "No access_log data yet" in text


class TestCliWiring(_TrendEnv):
    def test_cli_trend_command_reachable(self):
        import toolrecall.cli as cli_mod

        buf = io.StringIO()
        with redirect_stdout(buf), mock.patch.object(sys, "argv", ["toolrecall", "trend"]):
            cli_mod.cmd_trend()
        assert "Tokens NOT sent to the model" in buf.getvalue()

    def test_cli_trend_json_flag(self):
        base = self._day(0.05)
        self._seed(base, "/tmp/a.txt", hits=3, misses=1)
        import toolrecall.cli as cli_mod

        buf = io.StringIO()
        with (
            redirect_stdout(buf),
            mock.patch.object(sys, "argv", ["toolrecall", "trend", "--json"]),
        ):
            cli_mod.cmd_trend()
        data = json.loads(buf.getvalue())
        assert "trend" in data and "miss_classes" in data
