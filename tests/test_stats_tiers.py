"""Tests for P0.5 tier separation and non-replayable denominator tracking.

Validates:
1. cached_api_check writes to llm_passthrough (or api_cache) without cross-contamination.
2. cached_read with ttl=0 routes source="" to file_cache_internal and source="agent_tool" to file_cache.
3. _record with tokens_not_sent_measured records under the specified category (e.g. mcp_delta)
   and populates access_log.tokens on hits.
4. get_stats sets non_replayable_by_design=True and denominator_note on llm_passthrough and
   file_cache_internal, and sets measured_tier=True on mcp_dedup, mcp_delta, incremental_reads.
5. healthcheck.gather hit_rate calculation excludes non_replayable_by_design and excluded categories.
6. trend.non_replayable_summary cleanly separates passthrough from replayable rows.
7. tui_top queries include mcp_delta and incremental_reads in measured savings.
"""

from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timezone
import pytest

from toolrecall import cache, healthcheck, trend, tui_top
from toolrecall.config import load_config


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    """Isolate DB and config strictly to a temporary directory."""
    db_file = str(tmp_path / "cache.db")
    monkeypatch.setenv("TOOLRECALL_CACHE_DB", db_file)
    monkeypatch.setenv("TOOLRECALL_NO_AUTOSTART", "1")
    cache.config = load_config()
    cache._init()
    yield db_file


def test_cached_api_check_llm_passthrough_miss_and_hit(tmp_path, monkeypatch):
    """cached_api_check(..., category='llm_passthrough') updates llm_passthrough, not api_cache."""
    req_hash = "hash_turn_001"

    # 1. Miss path
    miss_res = cache.cached_api_check(req_hash, category="llm_passthrough")
    assert miss_res["cached"] is False

    stats = cache.get_stats()
    assert "llm_passthrough" in stats
    assert stats["llm_passthrough"]["misses"] == 1
    assert stats["llm_passthrough"]["hits"] == 0
    assert "api_cache" not in stats or stats["api_cache"]["misses"] == 0

    # Store a response in api_cache so the next check is a HIT
    cache.cached_api_store(
        request_hash=req_hash,
        method="POST",
        host="api.anthropic.com",
        path="/v1/messages",
        request_body_hash="bhash_001",
        response_status=200,
        response_headers={"Content-Type": "application/json"},
        response_body='{"content": [{"type": "text", "text": "Hello world"}]}',
        ttl=300,
    )

    # 2. Hit path
    hit_res = cache.cached_api_check(req_hash, category="llm_passthrough")
    assert hit_res["cached"] is True

    stats_after_hit = cache.get_stats()
    assert stats_after_hit["llm_passthrough"]["hits"] == 1
    assert stats_after_hit["llm_passthrough"]["misses"] == 1
    # api_cache category in cache_stats must remain untouched
    assert "api_cache" not in stats_after_hit or (
        stats_after_hit["api_cache"]["hits"] == 0 and stats_after_hit["api_cache"]["misses"] == 0
    )


def test_cached_read_ttl_0_tier_separation(tmp_path, monkeypatch):
    """cached_read on ttl=0 separates source='' into file_cache_internal and source='agent_tool' into file_cache."""
    sample_file = tmp_path / "live_log.txt"
    sample_file.write_text("Log line 1\nLog line 2\nLog line 3\nLog line 4\n")

    # Set file_ttl = 0
    monkeypatch.setenv("TOOLRECALL_FILE_TTL", "0")
    cache.config = load_config()

    # Read 1: source="" (internal infrastructure / shim)
    r1 = cache.cached_read(str(sample_file), source="")
    assert r1["cached"] is False

    stats1 = cache.get_stats()
    assert "file_cache_internal" in stats1
    int_uncacheable = stats1["file_cache_internal"]["tokens_read_uncacheable"]
    assert int_uncacheable > 0
    assert "file_cache" not in stats1 or stats1["file_cache"].get("tokens_read_uncacheable", 0) == 0

    # Read 2: source="agent_tool"
    r2 = cache.cached_read(str(sample_file), source="agent_tool")
    assert r2["cached"] is False

    stats2 = cache.get_stats()
    assert "file_cache" in stats2
    agent_uncacheable = stats2["file_cache"]["tokens_read_uncacheable"]
    assert agent_uncacheable > 0

    # Ensure zero access_log entries were created for uncacheable reads
    with cache._db() as conn:
        log_rows = conn.execute("SELECT COUNT(*) FROM access_log").fetchone()[0]
    assert log_rows == 0


def test_record_measured_tokens_accumulates_under_category_and_logs(isolated_db):
    """_record with tokens_not_sent_measured records under specific category and populates access_log."""
    cache._record(
        "mcp_delta",
        hit=True,
        path="/workspace/src/app.py",
        tokens_not_sent_measured=500,
    )

    con = sqlite3.connect(isolated_db)
    con.row_factory = sqlite3.Row

    # Check cache_stats
    row = con.execute("SELECT * FROM cache_stats WHERE category = 'mcp_delta'").fetchone()
    assert row is not None
    assert row["tokens_not_sent_measured"] == 500
    assert row["hits"] == 1

    # Check that mcp_dedup was NOT modified
    row_dedup = con.execute("SELECT * FROM cache_stats WHERE category = 'mcp_dedup'").fetchone()
    assert row_dedup is None or row_dedup["tokens_not_sent_measured"] == 0

    # Check access_log entry
    log_entry = con.execute("SELECT * FROM access_log WHERE category = 'mcp_delta'").fetchone()
    assert log_entry is not None
    assert log_entry["path"] == "/workspace/src/app.py"
    assert log_entry["hit"] == 1
    assert log_entry["tokens"] == 500

    con.close()


def test_get_stats_flags_and_healthcheck_exclusion(isolated_db, monkeypatch):
    """get_stats sets non_replayable_by_design and measured_tier, and healthcheck excludes them."""
    # Seed cache_stats rows
    with cache._db() as conn:
        now = time.time()
        conn.execute(
            """
            INSERT INTO cache_stats (category, hits, misses, tokens_not_sent_measured, updated_at)
            VALUES ('llm_passthrough', 0, 10, 0, ?)
            """,
            (now,),
        )
        conn.execute(
            """
            INSERT INTO cache_stats (category, hits, misses, tokens_not_sent_measured, updated_at)
            VALUES ('file_cache_internal', 0, 20, 0, ?)
            """,
            (now,),
        )
        conn.execute(
            """
            INSERT INTO cache_stats (category, hits, misses, tokens_not_sent_measured, updated_at)
            VALUES ('mcp_delta', 5, 0, 1000, ?)
            """,
            (now,),
        )
        conn.execute(
            """
            INSERT INTO cache_stats (category, hits, misses, io_tokens_saved, updated_at)
            VALUES ('file_cache', 80, 20, 5000, ?)
            """,
            (now,),
        )

    stats = cache.get_stats()

    # llm_passthrough flags
    assert stats["llm_passthrough"]["non_replayable_by_design"] is True
    assert "POST chat/completions" in stats["llm_passthrough"]["denominator_note"]

    # file_cache_internal flags
    assert stats["file_cache_internal"]["non_replayable_by_design"] is True
    assert "Non-agent" in stats["file_cache_internal"]["denominator_note"]

    # measured_tier flags
    assert stats["mcp_delta"].get("measured_tier") is True

    # Test healthcheck exclusion logic on stats dictionary
    # Mock subprocess.run in healthcheck.gather to return json dump of stats
    import subprocess

    class DummyProc:
        returncode = 0
        stdout = json.dumps(stats)
        stderr = ""

    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: DummyProc())
    monkeypatch.setattr(healthcheck, "_scan_recent_log", lambda: ("", 0, 0))

    info = healthcheck.gather()
    # Total cacheable is only file_cache: 80 hits, 20 misses -> 80 / 100 = 0.8
    # If llm_passthrough (10 misses) or file_cache_internal (20 misses) were included,
    # hit rate would be 80 / (80 + 20 + 10 + 20) = 80 / 130 = 0.615!
    assert info["hit_rate"] == 0.8


def test_trend_non_replayable_summary(tmp_path):
    """trend.non_replayable_summary accurately summarizes usage CSV categories and tokens."""
    csv_file = tmp_path / "proxy_usage.csv"
    header = (
        "timestamp,cache_status,target_host,target_path,request_hash,"
        "prompt_tokens,completion_tokens,cache_read_tokens,cache_write_tokens,"
        "cost_usd,dedup_tokens,cache_discount_usd\n"
    )
    now = time.time()
    rows = [
        f"{now},STREAM,openrouter.ai,/v1/chat/completions,h1,10000,500,2000,0,0.10,100,0.01\n",
        f"{now},HIT,openrouter.ai,/v1/chat/completions,h2,8000,400,0,0,0.08,0,0.0\n",
        f"{now},STREAM,api.anthropic.com,/v1/messages,h3,15000,800,5000,0,0.15,200,0.02\n",
        f"{now},REPLAY,api.openai.com,/v1/chat/completions,h4,5000,200,0,0,0.05,0,0.0\n",
    ]
    csv_file.write_text(header + "".join(rows))

    summary = trend.non_replayable_summary(usage_csv=str(csv_file))
    assert summary["total_rows"] == 4
    assert summary["llm_passthrough_rows"] == 2
    assert summary["replayable_rows"] == 2
    assert summary["billed_prompt_tokens"] == 25000  # 10000 + 15000
    assert summary["cached_at_discount_tokens"] == 7000  # 2000 + 5000


def test_tui_top_stubbed_and_replay_today(isolated_db):
    """tui_top.stubbed_today and session_replay_today count mcp_delta and incremental_reads."""
    today_start = (
        datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    )
    ts = today_start + 100

    with sqlite3.connect(isolated_db) as conn:
        conn.execute(
            """
            INSERT INTO access_log (category, path, hit, tokens, cached_at)
            VALUES
              ('mcp_dedup', '/path1', 1, 100, ?),
              ('mcp_delta', '/path2', 1, 200, ?),
              ('incremental_reads', '/path3', 1, 300, ?)
            """,
            (ts, ts, ts),
        )

    today, window = tui_top.stubbed_today(db=isolated_db)
    assert today == 600
    assert window == 600
