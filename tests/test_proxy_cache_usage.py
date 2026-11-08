"""Provider prefix-cache instrumentation (CSV v4): nested usage parsing.

OpenRouter reports cache reads in ``usage.prompt_tokens_details.cached_tokens``
(nested) and the discount in ``usage.cache_discount`` — the flat OpenAI/
Anthropic fields (``cache_read_input_tokens`` etc.) stay absent on that
route, which is why 44k rows logged cache_read=0 (2026-09-19 post-mortem)
while the discount was already billed inside ``usage.cost``. v4 adds the
``cache_discount_usd`` column and fills ``cache_read_tokens`` from the
nesting. Pre-v4 rows read 0 via DictReader ``.get() or 0`` (same back-compat
contract as cost_usd/dedup_tokens).

Render tests MUST restore a stubbed ``tui_top.money_today`` (try/finally, no
fixture dependencies): a bare assignment leaks into every later test in the
full-suite run — that was the 2026-09-19 gate failure (dedup/swallow tests
saw the stub's numbers).
"""

import csv
import os
import tempfile
import unittest


class TestProviderCacheUsage(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        import toolrecall.proxy as proxy

        self.proxy = proxy
        self.csv_path = os.path.join(self.tmpdir, "usage.csv")
        self._orig_path = proxy._USAGE_LOG_PATH
        proxy._USAGE_LOG_PATH = self.csv_path

    def tearDown(self):
        self.proxy._USAGE_LOG_PATH = self._orig_path

    def _rows(self):
        with open(self.csv_path) as f:
            return list(csv.DictReader(f))

    def test_openrouter_nested_fields_fill_columns(self):
        self.proxy._init_usage_log()
        self.proxy._log_proxy_usage(
            "STREAM",
            "openrouter.ai",
            "/api/v1/chat/completions",
            "abc123",
            usage={
                "prompt_tokens": 107041,
                "completion_tokens": 345,
                "cost": 0.002474982,
                "prompt_tokens_details": {"cached_tokens": 100864, "cache_write_tokens": 0},
                "cache_discount": 0.0041,
            },
        )
        row = self._rows()[0]
        self.assertEqual(row["cache_read_tokens"], "100864")
        self.assertEqual(row["cache_discount_usd"], "0.004100")

    def test_flat_fields_still_win(self):
        # OpenAI/Anthropic flat fields take precedence over the nesting.
        self.proxy._init_usage_log()
        self.proxy._log_proxy_usage(
            "STREAM",
            "api.anthropic.com",
            "/v1/messages",
            "abc123",
            usage={
                "prompt_tokens": 5000,
                "cache_read_tokens": 4000,
                "cache_write_tokens": 1000,
            },
        )
        row = self._rows()[0]
        self.assertEqual(row["cache_read_tokens"], "4000")
        self.assertEqual(row["cache_write_tokens"], "1000")
        self.assertEqual(row["cache_discount_usd"], "0.000000")

    def test_zero_when_no_cache_fields(self):
        self.proxy._init_usage_log()
        self.proxy._log_proxy_usage("STREAM", "h", "/p", "abc", usage={"prompt_tokens": 100})
        row = self._rows()[0]
        self.assertEqual(row["cache_read_tokens"], "0")
        self.assertEqual(row["cache_discount_usd"], "0.000000")

    def test_money_today_sums_cache_columns_ignores_pre_v4(self):
        import time as _t

        self.proxy._init_usage_log()
        now = _t.time()
        self.proxy._log_proxy_usage(
            "STREAM",
            "h",
            "/p",
            "a1",
            usage={
                "prompt_tokens": 10_000,
                "prompt_tokens_details": {"cached_tokens": 8_000},
                "cache_discount": 0.02,
                "cost": 0.05,
            },
        )
        self.proxy._log_proxy_usage(
            "STREAM",
            "h",
            "/p",
            "a2",
            usage={
                "prompt_tokens": 20_000,
                "prompt_tokens_details": {"cached_tokens": 4_000},
                "cache_discount": 0.01,
                "cost": 0.10,
            },
        )
        # v3-era row without the column → both new sums must ignore it.
        with open(self.csv_path, "a") as f:
            f.write(f"{now:.3f},STREAM,h,/p,old,30000,10,0,0,0.1,0\n")

        from toolrecall.tui_top import money_today

        m = money_today(self.csv_path)
        self.assertEqual(m["cache_read_tokens"], 12_000)
        self.assertAlmostEqual(m["cache_discount_usd"], 0.03, places=6)
        # dedup column (existing contract) unaffected.
        self.assertEqual(m["prompt"], 60_000)


class TestProviderCacheRender(unittest.TestCase):
    """Render contract with a stubbed money_today — restored in finally."""

    def setUp(self):
        from toolrecall import tui_top

        self._tui = tui_top
        self._orig_money = tui_top.money_today
        self.money = {
            "requests": 3,
            "prompt": 100_000,
            "completion": 500,
            "replays": 0,
            "median": 0,
            "dedup_tokens": 0,
            "cache_read_tokens": 90_000,
            "cache_discount_usd": 0.1234,
            "cost_usd": 0.5,
        }
        tui_top.money_today = lambda usage_csv=None: dict(self.money)  # type: ignore[assignment]
        self._snap = {"stats": {"_daemon": False}, "db": "/dev/null", "db_path": "/dev/null"}

    def tearDown(self):
        # Leak guard (2026-09-19 full-suite interference): ALWAYS restore.
        self._tui.money_today = self._orig_money

    def test_render_plain_shows_provider_prefix_cache(self):
        out = self._tui.render_plain(self._snap)
        self.assertIn("provider prefix-cache: 90.0K tok re-served by provider", out)
        self.assertIn("90.0% of sent", out)
        self.assertIn("discount $0.1234 (provider-reported)", out)
        # Bill-side effect stays out of the not-sent headline; the cache
        # line lives in Details.
        self.assertIn("SAVED TO LLM TODAY:  0 tok", out)
        self.assertIn("provider prefix-cache: 90.0K tok re-served by provider", out)

    def test_render_plain_hides_line_when_no_cache(self):
        self.money.update({"cache_read_tokens": 0, "cache_discount_usd": 0.0})
        out = self._tui.render_plain(self._snap)
        self.assertNotIn("provider prefix-cache", out)


if __name__ == "__main__":
    unittest.main()
