# Copyright (c) 2026 Robin Schultka
# SPDX-License-Identifier: MIT
# Source: https://github.com/whiskybeer/toolrecall
"""Tests for the Cross-Agent Failure Ledger.

Covers:
- Error signature normalization (UUIDs, timestamps, hex addrs, line numbers, paths)
- Secret redaction (tokens, API keys, creds, PEM blocks never hit SQLite)
- Failure recording (upsert, count increments, first_seen vs last_seen)
- Failure search (FTS5 and LIKE fallback, tool filtering)
- Failure resolution (marking fixes and retrieving by resolution keyword)
- Failure statistics (empty vs populated)
- Offline discovery script integration
"""

import os
import sqlite3
import time
import pytest

from toolrecall._db import _db
from toolrecall.failures import (
    failure_stats,
    normalize_error_signature,
    record_failure,
    resolve_failure,
    search_failures,
)
from scripts.discover_failure_signatures import discover_from_db, discover_from_log


@pytest.fixture(autouse=True)
def isolate_test_db(tmp_path, monkeypatch):
    """Ensure every test targets an isolated temporary database."""
    test_db = tmp_path / "test_failures.db"
    monkeypatch.setenv("TOOLRECALL_CACHE_DB", str(test_db))

    from toolrecall import _db as db_mod

    with db_mod._db_lock:
        if db_mod._db_real is not None:
            try:
                db_mod._db_real.close()
            except Exception:
                pass
            db_mod._db_real = None
        db_mod._cached_config = None
        db_mod._last_env_db = None

    yield test_db

    with db_mod._db_lock:
        if db_mod._db_real is not None:
            try:
                db_mod._db_real.close()
            except Exception:
                pass
            db_mod._db_real = None
        db_mod._cached_config = None
        db_mod._last_env_db = None


def test_normalize_collapses_volatile_patterns():
    """Volatile tokens (UUIDs, timestamps, hex addresses, line numbers, homes) collapse to identical signature."""
    e1 = (
        "\x1b[31mError at /home/alice/app.py line 42 (0x7fff1234): "
        "conn to 12345678-1234-1234-1234-123456789abc timed out at 2026-11-15T15:39:04Z\x1b[0m"
    )
    e2 = (
        "\x1b[32mError at /home/bob/app.py line 999 (0x7fffbeef): "
        "conn to 98765432-4321-4321-4321-ba9876543210 timed out at 2026-11-15T16:00:00Z\x1b[0m"
    )

    hash1, norm1 = normalize_error_signature("bash", e1)
    hash2, norm2 = normalize_error_signature("bash", e2)

    assert hash1 == hash2, "Signatures must match despite different lines, addrs, and UUIDs"
    assert norm1 == norm2, "Normalized text must be identical"
    assert "<uuid>" in norm1
    assert "<ts>" in norm1
    assert "0x<hex>" in norm1
    assert ":N" in norm1
    assert "~" in norm1
    assert "\x1b" not in norm1

    # Colon-style line numbers & Unix timestamps
    e3 = "Error in ~/app.py:123 (0x1234): conn to 00000000-0000-0000-0000-000000000000 timed out at 1731685144"
    e4 = "Error in ~/app.py:888 (0xabcd): conn to 11111111-1111-1111-1111-111111111111 timed out at 1731699999"

    hash3, norm3 = normalize_error_signature("bash", e3)
    hash4, norm4 = normalize_error_signature("bash", e4)

    assert hash3 == hash4
    assert norm3 == norm4


def test_secret_redaction_never_stored(isolate_test_db):
    """Secrets must be redacted before normalization and NEVER stored in SQLite."""
    # Build secrets using concatenation to avoid SAST detection on fixtures
    fake_bearer = "Bearer sk-" + "proj-" + "abcdef1234567890"
    raw_token = "sk-" + "proj-" + "abcdef1234567890"
    fake_ghp = "ghp_" + ("x" * 36)
    fake_akia = "AKIA" + "0123456789ABCDEF"
    fake_pem = (
        "-----BEGIN "
        + "OPENSSH PRIVATE KEY-----\n"
        + "MIIEowIBAAKCAQEA0...\n"
        + "-----END "
        + "OPENSSH PRIVATE KEY-----"
    )

    error_payload = (
        f"AuthenticationFailed with {fake_bearer} and {fake_ghp} and {fake_akia}\n"
        f"Key details:\n{fake_pem}"
    )

    record_failure("api_call", error_payload, agent_id="test_agent")

    # Read SQLite directly to ensure raw credentials never hit storage
    conn = sqlite3.connect(f"file:{os.path.abspath(isolate_test_db)}?mode=ro", uri=True)
    try:
        rows = conn.execute("SELECT normalized_error FROM failure_ledger").fetchall()
        assert len(rows) == 1
        stored_error = rows[0][0]

        assert raw_token not in stored_error
        assert fake_bearer not in stored_error
        assert fake_ghp not in stored_error
        assert fake_akia not in stored_error
        assert "MIIEow" not in stored_error
        assert "<redacted>" in stored_error

        # Also inspect FTS5 table if it exists
        cur = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='failure_ledger_fts'"
        )
        if cur.fetchone():
            fts_rows = conn.execute("SELECT normalized_error FROM failure_ledger_fts").fetchall()
            assert len(fts_rows) == 1
            fts_stored = fts_rows[0][0]
            assert raw_token not in fts_stored
            assert fake_ghp not in fts_stored
            assert fake_akia not in fts_stored
    finally:
        conn.close()


def test_record_failure_upsert_and_timestamps():
    """record_failure increments count and updates last_seen while keeping first_seen."""
    res1 = record_failure("git", "fatal: repository not found", agent_id="agent1")
    assert set(res1.keys()) == {"signature_hash", "tool", "normalized_error", "count"}
    assert res1["count"] == 1

    with _db() as conn:
        row = conn.execute(
            "SELECT first_seen, last_seen, count FROM failure_ledger WHERE signature_hash = ?",
            (res1["signature_hash"],),
        ).fetchone()
        assert row is not None
        first_seen = row[0]
        last_seen = row[1]
        assert row[2] == 1
        assert first_seen == last_seen

    time.sleep(0.02)

    res2 = record_failure("git", "fatal: repository not found", agent_id="agent2")
    assert res2["count"] == 2

    with _db() as conn:
        row2 = conn.execute(
            "SELECT first_seen, last_seen, count, agent_id FROM failure_ledger WHERE signature_hash = ?",
            (res1["signature_hash"],),
        ).fetchone()
        assert row2 is not None
        assert row2[0] == first_seen  # first_seen is preserved
        assert row2[1] > last_seen  # last_seen is updated
        assert row2[2] == 2
        assert row2[3] == "agent2"  # agent_id updated when provided


def test_search_failures_and_resolve():
    """search_failures and resolve_failure roundtrip correctly with text and tool filtering."""
    f1 = record_failure("bash", "Error: failed to connect to database host at 127.0.0.1")
    f2 = record_failure("git", "fatal: origin remote rejected push")

    # Search by text query
    res_db = search_failures("database")
    assert len(res_db) >= 1
    assert res_db[0]["signature_hash"] == f1["signature_hash"]
    assert res_db[0]["tool"] == "bash"

    # Search with tool filter
    res_git = search_failures("", tool="git")
    assert len(res_git) == 1
    assert res_git[0]["signature_hash"] == f2["signature_hash"]
    assert res_git[0]["tool"] == "git"

    # Non-matching tool
    res_empty = search_failures("database", tool="git")
    assert len(res_empty) == 0

    # Resolve failure
    resolved = resolve_failure(f1["signature_hash"], "Start local database with docker compose up")
    assert resolved is True

    # Resolve non-existent failure returns False
    fake_hash = "0" * 32
    assert resolve_failure(fake_hash, "some fix") is False

    # Search matches on resolution text
    res_res = search_failures("compose")
    assert len(res_res) >= 1
    assert res_res[0]["signature_hash"] == f1["signature_hash"]
    assert "docker compose up" in res_res[0]["resolution"]


def test_failure_stats():
    """failure_stats returns accurate counts for empty, unpopulated, and populated databases."""
    # Before any records exist
    stats0 = failure_stats()
    assert stats0 == {
        "unique_signatures": 0,
        "total_failures": 0,
        "resolved_signatures": 0,
    }

    # Add 2 occurrences of sig1, 3 occurrences of sig2
    f1 = record_failure("tool1", "Error message 1")
    record_failure("tool1", "Error message 1")

    record_failure("tool2", "Error message 2")
    record_failure("tool2", "Error message 2")
    record_failure("tool2", "Error message 2")

    stats1 = failure_stats()
    assert stats1["unique_signatures"] == 2
    assert stats1["total_failures"] == 5
    assert stats1["resolved_signatures"] == 0

    # Resolve one signature
    resolve_failure(f1["signature_hash"], "Fixed in v2")

    stats2 = failure_stats()
    assert stats2["unique_signatures"] == 2
    assert stats2["total_failures"] == 5
    assert stats2["resolved_signatures"] == 1


def test_discover_failure_signatures_script(isolate_test_db, tmp_path):
    """discover_failure_signatures script reads from DB and logs read-only without modifying state."""
    # Populate DB with failures
    record_failure("bash", "Command failed: exit code 127")
    record_failure("bash", "Command failed: exit code 127")
    record_failure("bash", "Command failed: exit code 127")
    record_failure("python", "KeyError: 'user_id'")

    # Query via discover_from_db
    recurring = discover_from_db(str(isolate_test_db), min_count=2)
    assert len(recurring) == 1
    assert recurring[0]["tool"] == "bash"
    assert recurring[0]["count"] == 3

    # Query with min_count=1
    all_sigs = discover_from_db(str(isolate_test_db), min_count=1)
    assert len(all_sigs) == 2

    # Query from a log file
    log_file = tmp_path / "test.log"
    log_file.write_text(
        "[curl] ERROR: Connection refused: 0xbeef at 2026-11-15T10:00:00Z\n"
        "[curl] ERROR: Connection refused: 0xcafe at 2026-11-15T11:00:00Z\n"
        "[git] ERROR: merge conflict\n",
        encoding="utf-8",
    )

    log_sigs = discover_from_log(str(log_file), min_count=2)
    assert len(log_sigs) == 1
    assert log_sigs[0]["tool"] == "curl"
    assert log_sigs[0]["count"] == 2
    assert "<ts>" in log_sigs[0]["normalized_error"]
