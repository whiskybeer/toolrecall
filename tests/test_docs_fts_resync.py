# Copyright (c) 2026 Robin Schultka
# SPDX-License-Identifier: MIT
# Source: https://github.com/whiskybeer/toolrecall
"""Re-indexing must keep the external-content FTS5 index usable.

Regression, found by the release gate: the upsert into ``pages`` used
``INSERT OR REPLACE``. SQLite fires DELETE triggers for the rows a REPLACE
constraint deletes **only when ``PRAGMA recursive_triggers`` is ON** (the
default is off), so ``pages_ad`` never ran, the FTS index kept a docid whose
``pages`` row was gone, and ``docs_search`` failed with::

    Search error: fts5: missing row 1 from content table 'main'.'pages'

That error outlived every retry because the self-heal path only recognised
``malformed``. The upserts now use ``ON CONFLICT(source, path) DO UPDATE``,
which keeps the rowid stable and fires ``pages_au`` instead.
"""

import sqlite3

from toolrecall import docs


def _pin_db(tmp_path, monkeypatch):
    db_file = tmp_path / "knowledge.db"
    monkeypatch.setenv("TOOLRECALL_KNOWLEDGE_DB", str(db_file))
    return db_file


def test_reindex_same_path_keeps_fts_in_sync(tmp_path, monkeypatch):
    """Re-indexing a changed file three times must keep search working."""
    _pin_db(tmp_path, monkeypatch)
    doc_dir = tmp_path / "docs"
    doc_dir.mkdir()
    doc = doc_dir / "guide.md"

    for revision in range(3):
        doc.write_text(
            f"# Guide rev{revision}\n\nrevision {revision} beta body\n", encoding="utf-8"
        )
        docs.index_directory(str(doc_dir), source="regress")

    res = docs.docs_search("beta")
    assert "Search error" not in res, res
    assert "rev2" in res, res


def test_legacy_desynced_db_stays_searchable(tmp_path, monkeypatch):
    """A DB left desynced by an older release must not hard-fail search.

    The raw upsert below rewrites exactly what <=0.8.24 wrote: a replaced
    content row (new rowid) whose FTS entry was never deleted.
    """
    db_file = _pin_db(tmp_path, monkeypatch)
    doc_dir = tmp_path / "docs"
    doc_dir.mkdir()
    (doc_dir / "guide.md").write_text("# Guide\n\nuniquetoken body\n", encoding="utf-8")
    docs.index_directory(str(doc_dir), source="regress")

    conn = sqlite3.connect(str(db_file))
    try:
        conn.execute("PRAGMA recursive_triggers = OFF")
        conn.execute(
            "INSERT OR REPLACE INTO pages (source, path, title, content, url) "
            "VALUES ('regress', 'guide.md', 'Guide rev9', 'uniquetoken body v9', '')"
        )
        conn.commit()
    finally:
        conn.close()

    res = docs.docs_search("uniquetoken")
    assert "Search error" not in res, res
    assert "Guide" in res


def test_desync_error_message_routes_to_self_heal():
    """The observed message must be recognised as repairable, not user error."""
    assert docs._is_fts_index_error("fts5: missing row 1 from content table 'main'.'pages'")
    assert docs._is_fts_index_error("database disk image is malformed")
    assert not docs._is_fts_index_error("no such table: pages")
