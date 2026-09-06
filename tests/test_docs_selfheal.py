"""Tests for the FTS self-heal rebuild path (_rebuild_fts_index).

Regression (2026-09): docs_search/docs_get_page 'malformed' recovery used
``except Exception: pass`` around the rebuild, so a rebuild that could not
run (daemon holding the DB open / WAL lock) silently no-oped and the caller
retried into the same malformed error forever. docs_get_page also recursed
without a retry guard (unbounded recursion risk).

Pins:
1. Successful rebuild returns True and actually fixes a corrupted index.
2. Failed rebuild returns False and logs loudly (no silent swallow).
3. docs_search/docs_get_page surface the failure instead of retry-looping.
4. The retry guard prevents unbounded recursion.
"""

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from toolrecall import docs as docs_mod
from toolrecall.docs import _rebuild_fts_index, docs_get_page, docs_search


@pytest.fixture()
def fts_db(tmp_path, monkeypatch):
    """A real knowledge DB with pages + FTS index at a tmp path."""
    db = tmp_path / "knowledge.db"
    monkeypatch.setenv("TOOLRECALL_KNOWLEDGE_DB", str(db))
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE pages (source TEXT, path TEXT, title TEXT, url TEXT, content TEXT)")
    conn.execute(
        "CREATE VIRTUAL TABLE pages_fts USING fts5(path, source, title, url, content,"
        " content='pages', content_rowid='rowid')"
    )
    conn.execute(
        "INSERT INTO pages (source, path, title, url, content)"
        " VALUES ('v', 'a.md', 'A', 'u', 'hello world content')"
    )
    conn.execute(
        "INSERT INTO pages_fts(rowid, path, source, title, url, content)"
        " SELECT rowid, path, source, title, url, content FROM pages"
    )
    conn.commit()
    conn.close()
    return str(db)


def _capture_stderr(fn, *args, **kwargs):
    import io

    buf = io.StringIO()
    orig = sys.stderr
    sys.stderr = buf
    try:
        result = fn(*args, **kwargs)
    finally:
        sys.stderr = orig
    return result, buf.getvalue()


class TestRebuildFtsIndex:
    def test_rebuild_succeeds_on_healthy_db(self, fts_db):
        ok, err = _capture_stderr(_rebuild_fts_index)
        assert ok is True
        assert "FAILED" not in err

    def test_rebuild_logs_loudly_on_failure(self, fts_db, monkeypatch):
        # Make the rebuild statement fail via a real DB-level fault:
        # revoke write access by opening the file through a read-only URI.
        real_connect = sqlite3.connect

        def ro_connect(path, *a, **kw):
            if "knowledge.db" in str(path):
                return real_connect(
                    f"file:{path}?mode=ro", uri=True, timeout=kw.get("timeout", 5.0)
                )
            return real_connect(path, *a, **kw)

        monkeypatch.setattr(docs_mod.sqlite3, "connect", ro_connect)
        ok, err = _capture_stderr(_rebuild_fts_index)
        assert ok is False
        assert "rebuild FAILED" in err

    def test_rebuild_recovers_corrupted_shadow_index(self, fts_db):
        """Corrupt the FTS shadow table, then verify rebuild heals it."""
        # Delete shadow rows behind sqlite's back → FTS queries go malformed
        conn = sqlite3.connect(fts_db)
        conn.execute(
            "DELETE FROM pages_fts_data WHERE rowid = (SELECT MIN(rowid) FROM pages_fts_data)"
        )
        conn.commit()
        conn.close()

        ok = _rebuild_fts_index()
        assert ok is True

        # Post-rebuild the full daemon-style query works
        conn = sqlite3.connect(fts_db)
        rows = conn.execute(
            "SELECT p.source, p.path FROM pages_fts f"
            " JOIN pages p ON p.path = f.path AND p.source = f.source"
            " WHERE pages_fts MATCH 'hello'"
        ).fetchall()
        conn.close()
        assert len(rows) == 1


class TestSelfHealCallers:
    def test_docs_search_reports_rebuild_failure(self, fts_db, monkeypatch):
        """When the rebuild fails, docs_search says so instead of retry-looping."""

        class MalformedConn:
            def execute(self, *a, **kw):
                raise sqlite3.DatabaseError("database disk image is malformed")

            def close(self):
                pass

        monkeypatch.setattr(docs_mod, "_get_db", lambda: MalformedConn())
        monkeypatch.setattr(docs_mod, "_rebuild_fts_index", lambda: False)
        result = docs_search("hello")
        assert "rebuild failed" in result
        assert "index-compact" in result

    def test_docs_search_retries_once_after_successful_rebuild(self, fts_db, monkeypatch):
        """First query malformed → rebuild → retry succeeds against real DB."""
        real_get_db = docs_mod._get_db
        rebuild_calls = []
        state = {"first": True}

        def flaky_db():
            if state["first"]:
                state["first"] = False

                class MalformedConn:
                    def execute(self, *a, **kw):
                        raise sqlite3.DatabaseError("database disk image is malformed")

                    def close(self):
                        pass

                return MalformedConn()
            return real_get_db()

        monkeypatch.setattr(docs_mod, "_get_db", flaky_db)

        def counting_rebuild():
            rebuild_calls.append(1)
            return True

        monkeypatch.setattr(docs_mod, "_rebuild_fts_index", counting_rebuild)
        result = docs_search("hello")
        assert len(rebuild_calls) == 1, "exactly one self-heal attempt"
        assert "Found 1 pages" in result

    def test_docs_get_page_reports_rebuild_failure(self, fts_db, monkeypatch):
        class MalformedConn:
            def execute(self, *a, **kw):
                raise sqlite3.DatabaseError("database disk image is malformed")

            def close(self):
                pass

        monkeypatch.setattr(docs_mod, "_get_db", lambda: MalformedConn())
        monkeypatch.setattr(docs_mod, "_rebuild_fts_index", lambda: False)
        result = docs_get_page("a.md", source="v")
        assert "rebuild failed" in result

    def test_get_page_no_unbounded_recursion(self, fts_db, monkeypatch):
        """A permanently malformed DB must terminate, not recurse forever."""
        count = {"n": 0}

        class MalformedConn:
            def execute(self, *a, **kw):
                count["n"] += 1
                raise sqlite3.DatabaseError("database disk image is malformed")

            def close(self):
                pass

        monkeypatch.setattr(docs_mod, "_get_db", lambda: MalformedConn())
        monkeypatch.setattr(docs_mod, "_rebuild_fts_index", lambda: True)
        result = docs_get_page("a.md", source="v")
        # One initial execute + one retry execute — then the retry guard
        # stops the loop and returns an error.
        assert count["n"] == 2, f"expected exactly 2 execute attempts, got {count['n']}"
        assert "malformed" in result.lower() or "Error" in result
