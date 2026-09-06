"""Tests for FTS index compaction: bloat measurement, compact_knowledge_db
losslessness, and the auto-compact threshold logic.

External-content FTS5 ('content=pages') accumulates deleted term data on
every re-index until an explicit rebuild. These tests verify compaction
reclaims it without touching any document text.

Isolation: hermetic config + tmp DB, same pattern as test_docs_refresh.py.
"""

from __future__ import annotations

import io
import os

import pytest

from toolrecall import docs as docs_mod
from toolrecall.docs import (
    _COMPACT_MIN_FTS_MB,
    _maybe_compact_index,
    compact_knowledge_db,
    get_index_bloat,
    index_directory,
    docs_search,
)


class _StubConfig:
    """Config stand-in that yields NO index sources (hermetic)."""

    def get(self, section, key, default=None):
        return default

    @property
    def agent_home(self) -> str:
        return "/nonexistent-agent-home"  # not $HOME, not ~/.hermes — deterministic


@pytest.fixture()
def fresh_db(tmp_path, monkeypatch):
    """Isolated knowledge DB + stubbed config."""
    db = tmp_path / "knowledge.db"
    monkeypatch.setenv("TOOLRECALL_KNOWLEDGE_DB", str(db))
    monkeypatch.setenv("TOOLRECALL_DOCS_INDEX_TTL", "0")
    monkeypatch.setattr(docs_mod, "_get_config", lambda: _StubConfig())
    yield db


def _seed_vault(tmp_path: str) -> str:
    """Create a small vault of markdown files."""
    vault = os.path.join(tmp_path, "vault")
    os.makedirs(vault, exist_ok=True)
    for i in range(5):
        with open(os.path.join(vault, f"note{i}.md"), "w") as f:
            f.write(f"# Note {i}\nUnique content token{i} with some filler text.\n" * 20)
    return vault


def test_get_index_bloat_empty(fresh_db):
    """No DB / no pages → (0, 0), no crash."""
    assert get_index_bloat() == (0, 0)
    index_directory(_make_empty_dir(), source="empty")
    fts, content = get_index_bloat()
    assert fts >= 0 and content == 0


def _make_empty_dir() -> str:
    d = os.path.join(os.environ.get("TMPDIR", "/tmp"), "empty_vault_test")
    os.makedirs(d, exist_ok=True)
    return d


def test_compact_reclaims_bloat_and_preserves_content(fresh_db, tmp_path):
    """Churn (repeated re-index with changed content) → compact → content
    intact, DB shrinks."""
    vault = _seed_vault(str(tmp_path))

    # Round 1: index original content
    index_directory(vault, source="v")
    before_fts, _ = get_index_bloat()
    assert before_fts > 0

    # Round 2-6: overwrite files with different content (INSERT OR REPLACE
    # churn → orphaned FTS term data)
    for gen in range(5):
        for i in range(5):
            p = os.path.join(vault, f"note{i}.md")
            with open(p, "w") as f:
                f.write(f"# Note {i} v{gen}\nGeneration{gen} token{i} replacement text.\n" * 20)
        index_directory(vault, source="v")

    # Search still works pre-compact
    r = docs_search("Generation4", source="v")
    assert "Generation4" in r

    result = compact_knowledge_db()
    assert result["after"] <= result["before"]
    assert result["reclaimed"] >= 0

    # Content intact after compact: old generation gone, new generation found
    assert "Generation4" in docs_search("Generation4", source="v")
    r_old = docs_search("Generation0", source="v")
    assert r_old.startswith("No results"), "old generation should be gone after re-index + compact"


def test_auto_compact_thresholds(tmp_path, fresh_db, monkeypatch):
    """_maybe_compact_index fires only when BOTH thresholds are exceeded."""

    # The gate runs pytest with -p no:capture (capsys unavailable), so we
    # patch sys.stdout directly to observe the compaction message.
    stdout_buf = io.StringIO()
    monkeypatch.setattr("sys.stdout", stdout_buf)

    vault = _seed_vault(str(tmp_path))
    index_directory(vault, source="v")
    fts, content = get_index_bloat()

    # Small DB: below _COMPACT_MIN_FTS_MB → never fires regardless of ratio
    monkeypatch.setattr(docs_mod, "_COMPACT_RATIO", 0.0001)
    _maybe_compact_index()
    assert "compacted" not in stdout_buf.getvalue()

    # Simulate bloat: monkeypatch get_index_bloat to report big FTS
    calls = {}

    def fake_compact():
        calls["ran"] = True
        return {"before": 100, "after": 10, "reclaimed": 90}

    big = _COMPACT_MIN_FTS_MB * 1024 * 1024 * 10
    monkeypatch.setattr(docs_mod, "get_index_bloat", lambda: (big, big // 100))
    monkeypatch.setattr(docs_mod, "compact_knowledge_db", fake_compact)

    # Ratio 5.0 default: big / (big//100) = 100 > 5 → fires
    _maybe_compact_index()
    assert calls.get("ran") is True

    # Ratio below actual → does not fire
    calls.clear()
    monkeypatch.setattr(docs_mod, "_COMPACT_RATIO", 1000.0)
    _maybe_compact_index()
    assert calls.get("ran") is None


def test_compact_idempotent(fresh_db, tmp_path):
    """Compact twice in a row: second pass is a no-op size-wise."""
    vault = _seed_vault(str(tmp_path))
    index_directory(vault, source="v")
    r1 = compact_knowledge_db()
    r2 = compact_knowledge_db()
    assert abs(r1["after"] - r2["after"]) < 0.05 * max(r1["after"], 1)
