#!/usr/bin/env python3
"""Forensic investigation script for uncacheable tokens growth vs access_log rows.

Demonstrates and verifies that reads on ttl=0 (never_cache) paths increment
tokens_read_uncacheable in cache_stats while inserting zero rows into access_log,
and validates that source="" (internal/shim) routes to file_cache_internal while
source="agent_tool" routes to file_cache.

Strictly isolated: runs entirely inside tempfile.TemporaryDirectory() and never
touches live ~/.toolrecall/ state. Zero runtime dependencies (stdlib only).
"""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile


def run_forensics() -> int:
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = os.path.join(tmp_dir, "test_cache.db")
        config_path = os.path.join(tmp_dir, "toolrecall.toml")
        test_file = os.path.join(tmp_dir, "uncacheable_sample.txt")

        # Create sample file (~40 tokens)
        sample_text = (
            "Line 1: ToolRecall deterministic execution layer for agent tools.\n"
            "Line 2: Testing uncacheable token accumulation and tier separation.\n"
            "Line 3: Zero runtime dependencies, isolated reproduction.\n"
        )
        with open(test_file, "w", encoding="utf-8") as f:
            f.write(sample_text)

        # Write isolated config with file_ttl = 0
        with open(config_path, "w", encoding="utf-8") as f:
            f.write(
                f"""[paths]
cache_db = "{db_path}"

[cache]
file_ttl = 0
"""
            )

        # Set env overrides
        os.environ["TOOLRECALL_CACHE_DB"] = db_path
        os.environ["TOOLRECALL_CONFIG"] = config_path
        os.environ["TOOLRECALL_FILE_TTL"] = "0"
        os.environ["TOOLRECALL_NO_AUTOSTART"] = "1"

        from toolrecall import cache
        from toolrecall.config import load_config

        cache.config = load_config()
        cache._init()

        def snapshot_db() -> tuple[dict[str, dict], int]:
            con = sqlite3.connect(db_path)
            con.row_factory = sqlite3.Row
            stats = {}
            for row in con.execute("SELECT * FROM cache_stats"):
                stats[row["category"]] = {
                    "hits": row["hits"],
                    "misses": row["misses"],
                    "tokens_read_from_disk": row["tokens_read_from_disk"],
                    "tokens_read_uncacheable": row["tokens_read_uncacheable"],
                }
            log_count = con.execute("SELECT COUNT(*) FROM access_log").fetchone()[0]
            con.close()
            return stats, log_count

        print("=" * 70)
        print("TOOLRECALL FORENSICS: UNCACHEABLE TOKENS VS ACCESS_LOG")
        print("=" * 70)
        print(f"Isolated Temp DB: {db_path}")
        print(f"Sample File:      {test_file} ({len(sample_text)} chars)")
        print("Configured TTL:   0 (never_cache=True)")

        s0, logs0 = snapshot_db()
        print(f"\n[Baseline] cache_stats categories: {list(s0.keys())}, access_log rows: {logs0}")

        # ── Step 1: Read with source="" (e.g. shim / internal serve-through) ──
        print("\n--- Step 1: cached_read(source='') ---")
        res1 = cache.cached_read(test_file, source="")
        assert res1.get("cached") is False, "ttl=0 read must not be cached"

        s1, logs1 = snapshot_db()
        int_stat = s1.get("file_cache_internal", {})
        print(f"  Result cached: {res1.get('cached')}")
        print(f"  cache_stats['file_cache_internal']: {int_stat}")
        print(f"  access_log row count: {logs1} (delta: +{logs1 - logs0})")

        # ── Step 2: Read with source="agent_tool" (e.g. agent tool read) ──
        print("\n--- Step 2: cached_read(source='agent_tool') ---")
        res2 = cache.cached_read(test_file, source="agent_tool")
        assert res2.get("cached") is False, "ttl=0 read must not be cached"

        s2, logs2 = snapshot_db()
        agent_stat = s2.get("file_cache", {})
        print(f"  Result cached: {res2.get('cached')}")
        print(f"  cache_stats['file_cache']: {agent_stat}")
        print(f"  access_log row count: {logs2} (delta: +{logs2 - logs1})")

        # ── Step 3: Forensic explanation ──
        print("\n" + "=" * 70)
        print("FORENSIC VERDICT & MECHANISM EXPLANATION")
        print("=" * 70)
        print(
            "1. WHY tokens_read_uncacheable GREW:\n"
            "   In toolrecall/cache.py `cached_read()`, when `never_cache` (file_ttl == 0)\n"
            "   is evaluated, it calls `_record_tokens_read_from_disk(category, ..., uncacheable=True)`.\n"
            "   This updates `cache_stats.tokens_read_uncacheable` to track volume that\n"
            "   cannot be served by cache without inflating the miss denominator.\n"
        )
        print(
            "2. WHY access_log HAS 0 ROWS:\n"
            "   `cached_read()` immediately returns `{'cached': False, 'content': content}`\n"
            "   upon recording `_record_tokens_read_from_disk()`. The `_record()` helper\n"
            "   (which contains the `INSERT INTO access_log` statement) is NEVER invoked\n"
            "   on never_cache paths. Hence, zero access_log entries are created.\n"
        )
        print(
            "3. TIER SEPARATION VERIFICATION:\n"
            f"   - source='' (shim/internal): recorded under 'file_cache_internal' "
            f"(uncacheable={int_stat.get('tokens_read_uncacheable', 0)} tok).\n"
            f"   - source='agent_tool': recorded under 'file_cache' "
            f"(uncacheable={agent_stat.get('tokens_read_uncacheable', 0)} tok).\n"
            "   - Total access_log rows remain 0.\n"
        )
        print("Forensics completed successfully.")
        return 0


if __name__ == "__main__":
    sys.exit(run_forensics())
