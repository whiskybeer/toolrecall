"""Tests for toolrecall top (TUI dashboard): data layer + plain rendering.

Hermetic: never touches the real daemon socket or the live cache.db —
uses tmp_path DBs seeded with the real schema, and monkeypatches the
daemon transport.
"""

import sqlite3
import time

import pytest

from toolrecall import tui_top


@pytest.fixture
def seeded_db(tmp_path, monkeypatch):
    """Minimal cache.db with the real schema + a few rows."""
    db = str(tmp_path / "cache.db")
    con = sqlite3.connect(db)
    con.executescript(
        """
        CREATE TABLE cache_stats (
            category TEXT PRIMARY KEY, hits INTEGER DEFAULT 0,
            misses INTEGER DEFAULT 0, tokens_read_from_disk INTEGER DEFAULT 0,
            tokens_saved INTEGER DEFAULT 0, context_tokens_saved INTEGER DEFAULT 0,
            updated_at REAL DEFAULT 0);
        CREATE TABLE access_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT, category TEXT NOT NULL,
            path TEXT, hit INTEGER NOT NULL, tokens INTEGER DEFAULT 0,
            cached_at REAL NOT NULL);
        """
    )
    con.execute(
        "INSERT INTO cache_stats VALUES ('file_cache', 80, 20, 0, 123456789, 3, ?)",
        (time.time() - 30,),
    )
    con.execute(
        "INSERT INTO access_log (category, path, hit, tokens, cached_at)"
        " VALUES ('file_cache', '/tmp/x.py', 1, 500, ?)",
        (time.time() - 5,),
    )
    con.commit()
    con.close()
    monkeypatch.setattr(tui_top, "DEFAULT_PATH", str(tmp_path / "no-sock.sock"))
    return db


def _stub_daemon(monkeypatch, enabled):
    monkeypatch.setattr(
        tui_top, "stats_via_daemon", (lambda: None) if not enabled else (lambda: {"_daemon": True})
    )


def test_sqlite_fallback_parses_stats(seeded_db, monkeypatch):
    _stub_daemon(monkeypatch, enabled=False)
    stats = tui_top.stats_via_sqlite(seeded_db)
    assert stats is not None
    assert stats["_daemon"] is False
    fc = stats["file_cache"]
    assert fc["hits"] == 80 and fc["misses"] == 20
    assert fc["hit_rate"] == "80%"


def test_snapshot_falls_back_when_daemon_down(seeded_db, monkeypatch):
    _stub_daemon(monkeypatch, enabled=False)
    snap = tui_top.snapshot_factory(seeded_db)
    assert snap["stats"]["_daemon"] is False


def test_access_rows_reads_log(seeded_db, monkeypatch):
    monkeypatch.setattr(tui_top, "DEFAULT_PATH", seeded_db + ".nonexistent")
    rows = tui_top.access_rows(seeded_db, limit=10)
    assert rows, "access tail should have rows"
    cat, path, hit, tokens, _ts = rows[-1]
    assert cat == "file_cache" and path == "/tmp/x.py" and hit == 1


def test_render_plain_contains_tables_and_tail(seeded_db, monkeypatch):
    _stub_daemon(monkeypatch, enabled=False)
    snap = tui_top.snapshot_factory(seeded_db)
    snap["log"] = tui_top.access_rows(seeded_db, limit=10)
    out = tui_top.render_plain(snap)
    assert "toolrecall top" in out
    assert "file_cache" in out and "80" in out and "6.3B" not in out
    assert "123.5M" in out  # 123456789 tokens
    assert "/tmp/x.py" in out and "HIT" in out


def test_daemon_path_flagged_live(seeded_db, monkeypatch):
    _stub_daemon(monkeypatch, enabled=True)
    snap = tui_top.snapshot_factory(seeded_db)
    assert snap["stats"]["_daemon"] is True


def test_human_tokens_and_age():
    assert tui_top.human_tokens(999) == "999"
    assert tui_top.human_tokens(12_400) == "12.4K"
    assert tui_top.human_tokens(6_337_264_909) == "6.3B"
    assert tui_top.age_str(0) == "-"
    assert tui_top.age_str(time.time() - 90) == "1m"


def test_main_once_prints_snapshot(seeded_db, monkeypatch):
    # NOTE: no capsys here — `make test-unit` runs with -p no:capture, which
    # disables the capture plugin that provides capsys/capfd.
    import io
    from contextlib import redirect_stdout

    _stub_daemon(monkeypatch, enabled=False)
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = tui_top.main(["--once", "--db", seeded_db])
    assert rc == 0
    assert "file_cache" in buf.getvalue()


# ─── Interactive layer ───────────────────────────────────────


def test_view_names_and_context_view_exist():
    # 2026-09-16: view order swapped for end users — 1 = saved (money +
    # measured), 4 = per-cache IO stats (tech).
    assert tui_top.VIEW_NAMES == ["context", "log", "trend", "stats", "help"]
    assert tui_top.context_via_daemon is not None
    assert tui_top.action_refresh_file is not None
    assert tui_top.action_set_checkpoint is not None


def test_context_via_daemon_unreachable_returns_none(seeded_db, monkeypatch):
    # DEFAULT_PATH points at a nonexistent socket (fixture) -> None, no raise
    assert tui_top.context_via_daemon() is None


def test_render_plain_shows_context_when_daemon_up(seeded_db, monkeypatch):
    _stub_daemon(monkeypatch, enabled=True)
    monkeypatch.setattr(
        tui_top,
        "context_via_daemon",
        lambda: {
            "checkpoint": 3,
            "total_dirty": 1,
            "total_clean": 5,
            "total_stale": 1,
            "est_reclaimable_tokens": 2048,
            "stale": [{"path": "/tmp/stale.py", "est_tokens": 2048}],
        },
    )
    out = tui_top.render_plain(tui_top.snapshot_factory(seeded_db))
    # Contract since 2026-09-16: the banked "ctx dropped" ledger is NOT
    # rendered for end users; stale-file warnings still are.
    assert "/tmp/stale.py" in out
    assert "ctx dropped" not in out


def test_action_error_paths_are_soft(seeded_db, monkeypatch):
    # Daemon unreachable -> (False, msg), never raises
    ok, msg = tui_top.action_refresh_file("/tmp/nope.txt")
    assert ok is False and "unavailable" in msg or "fail" in msg.lower()
    ok2, msg2 = tui_top.action_set_checkpoint()
    assert ok2 is False and msg2
