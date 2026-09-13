"""Tests for tracker persistence (option A): snapshot / restore via _db().

Covers:
  - snapshot() writes a tracker_state row; save-then-restore round-trips
    read set, dirty map, checkpoint counter, armed flag, banked tokens
  - TTL gate: expired snapshots are refused ("ttl-expired"); ttl=0 disables
  - mtime re-validation: files modified after the snapshot are dropped
    from the restored read set (never reported clean)
  - sensitive paths are never restored
  - reset() clears the persisted row (no resurrection on next restore)
  - snapshot/restore are fail-soft: DB errors never raise
  - restore() composes with the banked-once ctx_dropped_tokens guard
"""

import os
import sys
import tempfile
import time
import warnings

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from toolrecall.context_tracker import ContextTracker


def _make(content: str = "x = 1\n", path: str = None) -> str:
    if path is None:
        f = tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".py")
        f.write(content)
        f.close()
        return f.name
    with open(path, "w") as fh:
        fh.write(content)
    return path


def _point_db(path) -> None:
    """Point the _db() singleton at `path`, the IsolatedDB way.

    Sets BOTH levers: the env var (raw source of truth — _db() reloads the
    config when the raw env changes, 2026-09-13 late-pin fix) AND patches
    the cached config's paths.cache_db. Then drops the singleton so the
    next _db() reopens on the new path.
    """
    from toolrecall import _db as db_mod

    os.environ["TOOLRECALL_CACHE_DB"] = str(path)
    cfg = db_mod._get_cached_config()
    cfg._data.setdefault("paths", {})["cache_db"] = str(path)
    if db_mod._db_real is not None:
        db_mod._db_real.close()
    db_mod._db_real = None
    db_mod._db_path_cached = None


@pytest.fixture()
def fresh_db(monkeypatch, tmp_path):
    """Isolated cache DB per test (conftest already redirects, but force it).

    Also resets _db()'s singleton connection + cached config path, the
    same way test_daemon_error_honesty's IsolatedDB does — otherwise the
    cached connection still points at the PREVIOUS test's DB path and
    "no-state" assertions see the previous test's tracker_state row.
    """
    monkeypatch.setenv("TOOLRECALL_DOCS_INDEX_TTL", "0")
    _point_db(tmp_path / "tracker-persist.db")
    yield
    from toolrecall import _db as db_mod

    if db_mod._db_real is not None:
        db_mod._db_real.close()
        db_mod._db_real = None
        db_mod._db_path_cached = None


class TestSnapshotRoundTrip:
    def test_snapshot_saves_row(self, fresh_db):
        t = ContextTracker()
        p = _make()
        t.mark_read(p)
        r = t.snapshot()
        assert r["saved"] is True
        assert r["paths"] == 1
        assert r["dirty"] == 0

    def test_restore_returns_state(self, fresh_db):
        p1, p2 = _make(), _make("y = 2\n")
        t = ContextTracker()
        t.mark_read(p1)
        t.mark_read(p2)
        t.mark_dirty(p1)
        t.set_checkpoint("before-restart")
        t.snapshot()

        t2 = ContextTracker()  # fresh "daemon boot"
        res = t2.restore(ttl=3600)
        assert res["restored"] is True
        assert res["reason"] == "ok"

        st = t2.get_stats()
        assert st["total_read"] == 2
        assert st["total_dirty"] == 1
        assert st["checkpoint"] == 1
        # dirty file is p1, read-only file is clean
        assert p1 in st["dirty"]
        assert p2 in st["clean"]

    def test_restore_preserves_armed_flag(self, fresh_db):
        p = _make()
        t = ContextTracker()
        t.mark_read(p)
        t.set_checkpoint("agent drove this")
        t.snapshot()

        t2 = ContextTracker()
        t2.restore(ttl=3600)
        assert t2.get_stats()["agent_armed"] is True

    def test_restore_no_state(self, fresh_db):
        t = ContextTracker()
        res = t.restore(ttl=3600)
        assert res["restored"] is False
        assert res["reason"] == "no-state"

    def test_ttl_expiry_blocks_restore(self, fresh_db):
        p = _make()
        t = ContextTracker()
        t.mark_read(p)
        t.snapshot()
        # Backdate the row past the TTL.
        from toolrecall._db import _db

        with _db() as conn:
            conn.execute("UPDATE tracker_state SET saved_at = ?", (time.time() - 70000,))
        t2 = ContextTracker()
        res = t2.restore(ttl=3600)
        assert res["restored"] is False
        assert res["reason"] == "ttl-expired"
        assert t2.get_stats()["total_read"] == 0

    def test_ttl_zero_disables_restore(self, fresh_db):
        p = _make()
        t = ContextTracker()
        t.mark_read(p)
        t.snapshot()
        t2 = ContextTracker()
        res = t2.restore(ttl=0)
        assert res["restored"] is False
        assert res["reason"] == "disabled"

    def test_mtime_change_drops_read_entry(self, fresh_db):
        p_kept = _make("keep = 1\n")
        p_mod = _make("mod = 1\n")
        t = ContextTracker()
        t.mark_read(p_kept)
        t.mark_read(p_mod)
        t.snapshot()
        time.sleep(0.01)
        os.utime(p_mod, None)  # touched after the snapshot

        t2 = ContextTracker()
        res = t2.restore(ttl=3600)
        assert res["restored"] is True
        assert p_kept in t2._read_set
        assert p_mod not in t2._read_set  # no longer what the agent saw

    def test_deleted_file_dropped_from_read_set(self, fresh_db):
        p_kept = _make("keep = 1\n")
        p_gone = _make("gone = 1\n")
        t = ContextTracker()
        t.mark_read(p_kept)
        t.mark_read(p_gone)
        t.snapshot()
        os.unlink(p_gone)

        t2 = ContextTracker()
        t2.restore(ttl=3600)
        assert p_kept in t2._read_set
        assert p_gone not in t2._read_set

    def test_dirty_entry_kept_even_if_file_deleted(self, fresh_db):
        p = _make("w = 1\n")
        t = ContextTracker()
        t.mark_dirty(p)
        t.snapshot()
        os.unlink(p)

        t2 = ContextTracker()
        res = t2.restore(ttl=3600)
        assert res["restored"] is True
        assert p in t2.get_stats()["dirty"]

    def test_sensitive_paths_not_restored(self, fresh_db, monkeypatch, tmp_path):
        # Build a read set containing a path the sensitive-pattern list hits.
        p_safe = _make("safe = 1\n")
        p_sensitive = os.path.join(str(tmp_path), ".env.secrets")
        _make("SECRET=1\n", path=p_sensitive)
        t = ContextTracker()
        t.mark_read(p_safe)
        t.mark_read(p_sensitive)
        t.snapshot()

        t2 = ContextTracker()
        t2.restore(ttl=3600)
        assert p_safe in t2._read_set
        assert p_sensitive not in t2._read_set


class TestResetAndPersistence:
    def test_reset_clears_persisted_row(self, fresh_db):
        p = _make()
        t = ContextTracker()
        t.mark_read(p)
        t.snapshot()
        t.reset()

        t2 = ContextTracker()
        res = t2.restore(ttl=3600)
        assert res["restored"] is False
        assert res["reason"] == "no-state"

    def test_clear_persisted_idempotent(self, fresh_db):
        t = ContextTracker()
        assert t.clear_persisted() is False  # nothing to clear
        p = _make()
        t.mark_read(p)
        t.snapshot()
        assert t.clear_persisted() is True
        assert t.clear_persisted() is False

    def test_clear_persisted_without_table_is_silent(self, fresh_db):
        # Never snapshotted: the tracker_state table may not exist yet —
        # reset() must not spam a "no such table" warning.
        t = ContextTracker()
        with warnings.catch_warnings():
            warnings.simplefilter("error")  # any warning fails the test
            t.reset()

    def test_reset_after_snapshot_then_snapshot_again(self, fresh_db):
        p = _make()
        t = ContextTracker()
        t.mark_read(p)
        t.snapshot()
        t.reset()
        t.mark_read(p)
        r = t.snapshot()
        assert r["saved"] is True and r["paths"] == 1
        t2 = ContextTracker()
        assert t2.restore(ttl=3600)["restored"] is True


class TestBankedTokensAcrossRestart:
    def test_banked_guard_survives_restore(self, fresh_db):
        """ctx_dropped_tokens total must not re-inflate after restore."""
        p_clean = _make("clean = 'file content'\n")  # read, never dirtied
        p_written = _make("written = 1\n")  # dirtied pre-checkpoint
        t = ContextTracker()
        t.mark_read(p_clean)
        t.mark_read(p_written)
        t.mark_dirty(p_written)
        t.set_checkpoint("c1")  # banks nothing (both files dirty or in dirty set)
        t.get_dirty()  # p_written turns clean here → banked once
        total_before = t.get_stats()["ctx_dropped_tokens_total"]
        t.snapshot()

        t2 = ContextTracker()
        t2.restore(ttl=3600)
        t2.get_dirty()
        total_after = t2.get_stats()["ctx_dropped_tokens_total"]
        # Exactly the pre-restart total: the same files banked once overall,
        # never once per process lifetime.
        assert total_after == total_before

    def test_unbanked_read_set_banks_exactly_once_after_restore(self, fresh_db):
        p = _make("some content\n" * 10)
        t = ContextTracker()
        t.mark_read(p)
        t.snapshot()  # nothing banked yet (no checkpoint/get_dirty called)

        t2 = ContextTracker()
        t2.restore(ttl=3600)
        first = t2.get_stats()["ctx_dropped_tokens_total"]
        t2.get_dirty()
        second = t2.get_stats()["ctx_dropped_tokens_total"]
        # The daemon-start checkpoint banks it exactly once — not twice.
        assert second == first or second - first == os.path.getsize(p) // 4


class TestFailSoft:
    def test_restore_on_unreadable_db_never_raises(self, monkeypatch, tmp_path):
        monkeypatch.setenv("TOOLRECALL_DOCS_INDEX_TTL", "0")
        # Corrupt the DB file so _db() init fails.
        db_path = tmp_path / "broken.db"
        db_path.write_bytes(b"this is not a sqlite database\x00\x01\x02")
        _point_db(db_path)
        t = ContextTracker()
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            res = t.restore(ttl=3600)
        assert res["restored"] is False
        assert res["reason"] in ("error", "no-state")

    def test_snapshot_reports_failure_not_raise(self, monkeypatch, tmp_path):
        monkeypatch.setenv("TOOLRECALL_DOCS_INDEX_TTL", "0")
        db_path = tmp_path / "broken.db"
        db_path.write_bytes(b"this is not a sqlite database\x00\x01\x02")
        _point_db(db_path)
        t = ContextTracker()
        t.mark_read(_make())
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            r = t.snapshot()
        assert r["saved"] is False
        assert r["paths"] == 1  # counts still reported honestly


class TestConfigTTL:
    def test_default_ttl_is_6h(self, fresh_db):
        from toolrecall.config import Config

        cfg = Config.__new__(Config)
        cfg._data = {}
        assert cfg.tracker_persist_ttl == 21600.0

    def test_ttl_from_toml_value(self, fresh_db):
        from toolrecall.config import Config

        cfg = Config()
        cfg._data = {"tracker": {"persist_ttl": 600}}
        assert cfg.tracker_persist_ttl == 600.0

    def test_ttl_invalid_falls_back_to_default(self, fresh_db):
        from toolrecall.config import Config

        cfg = Config()
        cfg._data = {"tracker": {"persist_ttl": "not-a-number"}}
        assert cfg.tracker_persist_ttl == 21600.0

    def test_env_var_override(self, fresh_db, monkeypatch):
        monkeypatch.setenv("TOOLRECALL_TRACKER_PERSIST_TTL", "120")
        from toolrecall.config import load_config

        assert load_config().tracker_persist_ttl == 120.0
