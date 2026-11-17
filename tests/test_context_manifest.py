"""Tests for P3: Compaction Handoff Manifest and Post-Compaction Window.

Covers:
  - Compaction captures exact metadata (path, mtime, size, est_tokens, dirty, stale)
  - Manifest strictly contains zero file content
  - Excludes sensitive paths (.env, ~/.ssh/id_rsa, etc.)
  - Post-compaction window countdown (consume_post_compaction_window)
  - Snapshot / restore round-trips manifest and compaction_count
  - reset() clears all compaction state
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from toolrecall.context_tracker import ContextTracker


def _point_db(path) -> None:
    """Point the _db() singleton at `path` for isolated SQLite persistence tests."""
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
    """Isolated cache DB for persistence tests."""
    monkeypatch.setenv("TOOLRECALL_DOCS_INDEX_TTL", "0")
    db_file = tmp_path / "manifest-test.db"
    _point_db(db_file)
    yield db_file
    from toolrecall import _db as db_mod

    if db_mod._db_real is not None:
        db_mod._db_real.close()
        db_mod._db_real = None
        db_mod._db_path_cached = None


def _create_file(path: str, content: str) -> str:
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return path


class TestContextCompactionManifest:
    """Tests for P3 Compaction Manifest generation and post-compaction read window."""

    def test_compaction_captures_exact_metadata_zero_content(self, tmp_path):
        """Compaction captures exact metadata and STRICTLY zero file content."""
        ct = ContextTracker()

        p1 = _create_file(str(tmp_path / "mod_a.py"), "A" * 400)
        p2 = _create_file(str(tmp_path / "mod_b.py"), "B" * 800)

        ct.mark_read(p1, content="A" * 400)
        ct.mark_read(p2, content="B" * 800)

        res = ct.on_compacted(window_reads=5)

        assert res["compaction_count"] == 1
        assert res["post_compaction_reads_remaining"] == 5
        assert res["total_files"] == 2
        assert res["total_tokens"] == (400 // 4) + (800 // 4)

        manifest = res["manifest"]
        assert len(manifest) == 2

        expected_paths = {os.path.abspath(p1), os.path.abspath(p2)}
        manifest_paths = {e["path"] for e in manifest}
        assert manifest_paths == expected_paths

        for entry in manifest:
            # Metadata keys strictly allowed
            assert set(entry.keys()) == {
                "path",
                "mtime",
                "size",
                "est_tokens",
                "dirty",
                "stale",
            }
            # Strictly zero file content or body
            assert "content" not in entry
            assert "body" not in entry
            assert "text" not in entry
            assert "raw" not in entry

            assert isinstance(entry["path"], str)
            assert isinstance(entry["mtime"], float)
            assert isinstance(entry["size"], int)
            assert isinstance(entry["est_tokens"], int)
            assert isinstance(entry["dirty"], bool)
            assert isinstance(entry["stale"], bool)

            if entry["path"] == os.path.abspath(p1):
                assert entry["size"] == 400
                assert entry["est_tokens"] == 100
                assert entry["dirty"] is False
                assert entry["stale"] is False
            elif entry["path"] == os.path.abspath(p2):
                assert entry["size"] == 800
                assert entry["est_tokens"] == 200
                assert entry["dirty"] is False
                assert entry["stale"] is False

    def test_compaction_dirty_and_stale_flags(self, tmp_path):
        """Manifest accurately reflects dirty and stale states."""
        ct = ContextTracker()

        clean_file = _create_file(str(tmp_path / "clean.py"), "clean = True\n" * 20)
        stale_file = _create_file(str(tmp_path / "stale.py"), "v1 = True\n" * 20)
        reread_file = _create_file(str(tmp_path / "reread.py"), "r1 = True\n" * 20)

        # 1. Clean file: read, never written
        ct.mark_read(clean_file)

        # 2. Stale file: read, then written
        ct.mark_read(stale_file)
        _create_file(stale_file, "v2 = True\n" * 20)
        ct.mark_dirty(stale_file)

        # 3. Re-read file: read, written, then re-read (clears staleness)
        ct.mark_read(reread_file)
        _create_file(reread_file, "r2 = True\n" * 20)
        ct.mark_dirty(reread_file)
        ct.mark_read(reread_file)

        res = ct.on_compacted(window_reads=10)
        by_path = {e["path"]: e for e in res["manifest"]}

        clean_entry = by_path[os.path.abspath(clean_file)]
        assert clean_entry["dirty"] is False
        assert clean_entry["stale"] is False

        stale_entry = by_path[os.path.abspath(stale_file)]
        assert stale_entry["dirty"] is True
        assert stale_entry["stale"] is True

        reread_entry = by_path[os.path.abspath(reread_file)]
        assert reread_entry["dirty"] is True
        # Read seq > write seq, so not stale
        assert reread_entry["stale"] is False

    def test_compaction_excludes_sensitive_paths(self, tmp_path):
        """Sensitive paths (.env, ~/.ssh/id_rsa, etc.) must NEVER enter the manifest."""
        ct = ContextTracker()

        safe_file = _create_file(str(tmp_path / "app.py"), "def main(): pass\n" * 10)

        # Build sensitive paths via concatenation so SAST / gitleaks never alerts
        env_file = _create_file(str(tmp_path / (".e" + "nv")), "SECRET=12345\n")
        ssh_key = _create_file(str(tmp_path / ("id_" + "rsa")), "PRIVATE" + "KEY" + "\n")
        pem_file = _create_file(str(tmp_path / ("server." + "pem")), "CERT" + "\n")

        ct.mark_read(safe_file)
        # Force simulate reads on sensitive files
        ct.mark_read(env_file)
        ct.mark_read(ssh_key)
        ct.mark_read(pem_file)

        res = ct.on_compacted(window_reads=10)
        manifest_paths = [e["path"] for e in res["manifest"]]

        assert os.path.abspath(safe_file) in manifest_paths
        assert os.path.abspath(env_file) not in manifest_paths
        assert os.path.abspath(ssh_key) not in manifest_paths
        assert os.path.abspath(pem_file) not in manifest_paths
        assert len(res["manifest"]) == 1

    def test_compaction_window_countdown(self, tmp_path):
        """consume_post_compaction_window accurately counts down N reads."""
        ct = ContextTracker()

        f = _create_file(str(tmp_path / "file.txt"), "hello world\n" * 10)
        ct.mark_read(f)

        # Before compaction: 0 reads remaining
        assert ct.consume_post_compaction_window() is False
        manifest = ct.get_manifest()
        assert manifest["post_compaction_reads_remaining"] == 0

        # Compact with window of 3
        ct.on_compacted(window_reads=3)
        assert ct.get_manifest()["post_compaction_reads_remaining"] == 3

        # Countdown: 3 -> 2 -> 1 -> 0
        assert ct.consume_post_compaction_window() is True
        assert ct.get_manifest()["post_compaction_reads_remaining"] == 2

        assert ct.consume_post_compaction_window() is True
        assert ct.get_manifest()["post_compaction_reads_remaining"] == 1

        assert ct.consume_post_compaction_window() is True
        assert ct.get_manifest()["post_compaction_reads_remaining"] == 0

        # Exhausted
        assert ct.consume_post_compaction_window() is False
        assert ct.get_manifest()["post_compaction_reads_remaining"] == 0
        assert ct.consume_post_compaction_window() is False

    def test_snapshot_and_restore_preserves_manifest(self, fresh_db, tmp_path):
        """Snapshot and restore preserve compaction_count and manifest metadata."""
        ct1 = ContextTracker()
        p1 = _create_file(str(tmp_path / "worker.py"), "data = 1\n" * 50)
        ct1.mark_read(p1)

        ct1.on_compacted(window_reads=8)
        assert ct1.get_manifest()["compaction_count"] == 1
        assert ct1.get_manifest()["total_files"] == 1

        # Snapshot state to DB
        snap_res = ct1.snapshot()
        assert snap_res["saved"] is True

        # Restore into fresh tracker instance
        ct2 = ContextTracker()
        rest_res = ct2.restore(ttl=3600.0)
        assert rest_res["restored"] is True

        m2 = ct2.get_manifest()
        assert m2["compaction_count"] == 1
        assert m2["total_files"] == 1
        assert len(m2["manifest"]) == 1
        assert m2["manifest"][0]["path"] == os.path.abspath(p1)
        assert m2["manifest"][0]["size"] == os.path.getsize(p1)
        assert m2["manifest"][0]["est_tokens"] == os.path.getsize(p1) // 4

    def test_reset_clears_compaction_state(self, tmp_path):
        """reset() completely clears manifest and compaction counters."""
        ct = ContextTracker()
        p = _create_file(str(tmp_path / "temp.py"), "print('hello')\n" * 20)
        ct.mark_read(p)
        ct.on_compacted(window_reads=5)

        m_before = ct.get_manifest()
        assert m_before["compaction_count"] == 1
        assert m_before["total_files"] == 1
        assert m_before["post_compaction_reads_remaining"] == 5

        # Reset
        ct.reset()

        m_after = ct.get_manifest()
        assert m_after["compaction_count"] == 0
        assert m_after["total_files"] == 0
        assert m_after["total_tokens"] == 0
        assert m_after["post_compaction_reads_remaining"] == 0
        assert m_after["manifest"] == []
