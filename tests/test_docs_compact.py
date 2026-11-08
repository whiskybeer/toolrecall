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

    def fake_compact(vacuum=True):
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


# ── Configurable auto-compact ([docs] keys + env plumbing) ──────────────────


class _DictConfig:
    """Config stand-in backed by a dict of (section, key) → value."""

    def __init__(self, values=None):
        self._values = dict(values or {})

    def get(self, section, key, default=None):
        return self._values.get((section, key), default)

    @property
    def agent_home(self) -> str:
        return "/nonexistent-agent-home"  # deterministic, like _StubConfig


def test_compact_disabled_via_config(fresh_db, monkeypatch):
    """[docs].compact = false → auto-compact never fires, even past thresholds."""
    monkeypatch.setattr(docs_mod, "_get_config", lambda: _DictConfig({("docs", "compact"): False}))
    monkeypatch.setattr(docs_mod, "_COMPACT_RATIO", 0.0001)
    monkeypatch.setattr(docs_mod, "_COMPACT_MIN_FTS_MB", 0.0001)
    calls = {}
    monkeypatch.setattr(
        docs_mod,
        "compact_knowledge_db",
        lambda vacuum=True: (
            calls.setdefault("ran", True) or {"before": 1, "after": 1, "reclaimed": 0}
        ),
    )
    _maybe_compact_index()
    assert calls.get("ran") is None


def test_compact_thresholds_via_config(fresh_db, tmp_path, monkeypatch):
    """Configured compact_ratio / compact_min_fts_mb / compact_vacuum win."""
    vault = _seed_vault(str(tmp_path))
    index_directory(vault, source="v")

    calls = {}
    monkeypatch.setattr(
        docs_mod,
        "compact_knowledge_db",
        lambda vacuum=True: (
            calls.setdefault("ran", vacuum) or {"before": 1, "after": 1, "reclaimed": 0}
        ),
    )

    # Default thresholds (50 MB / ×5) → the tiny vault never fires.
    _maybe_compact_index()
    assert calls.get("ran") is None

    # Config lowers both thresholds → fires; vacuum=False is threaded through.
    cfg = _DictConfig(
        {
            ("docs", "compact_ratio"): 0.0001,
            ("docs", "compact_min_fts_mb"): 0.0001,
            ("docs", "compact_vacuum"): False,
        }
    )
    monkeypatch.setattr(docs_mod, "_get_config", lambda: cfg)
    _maybe_compact_index()
    assert calls.get("ran") is False


def test_cfg_float_fails_soft(fresh_db):
    """Unparseable configured thresholds fall back to the default."""
    bad = _DictConfig({("docs", "compact_ratio"): "garbage", ("docs", "compact_min_fts_mb"): None})
    assert docs_mod._cfg_float(bad, "compact_ratio", 5.0) == 5.0
    assert docs_mod._cfg_float(bad, "compact_min_fts_mb", 50.0) == 50.0
    good = _DictConfig({("docs", "compact_ratio"): 2.5})
    assert docs_mod._cfg_float(good, "compact_ratio", 5.0) == 2.5


def test_compact_rebuild_without_vacuum(fresh_db, tmp_path):
    """vacuum=False: FTS rebuilt (orphaned terms reclaimed), content intact,
    the DB file itself is not rewritten."""
    vault = _seed_vault(str(tmp_path))
    index_directory(vault, source="v")
    for gen in range(5):
        for i in range(5):
            p = os.path.join(vault, f"note{i}.md")
            with open(p, "w") as f:
                f.write(f"# Note {i} v{gen}\nGeneration{gen} token{i} replacement text.\n" * 20)
        index_directory(vault, source="v")

    fts_before, _ = get_index_bloat()
    compact_knowledge_db(vacuum=False)
    fts_after, _ = get_index_bloat()
    assert fts_after <= fts_before
    # Content intact after rebuild-only compact
    assert "Generation4" in docs_search("Generation4", source="v")
    assert docs_search("Generation0", source="v").startswith("No results")


def test_env_map_wiring(monkeypatch, tmp_path):
    """The four [docs] compaction keys are ENV-plumbed with correct types."""
    from toolrecall.config import ENV_MAP, load_config

    assert ENV_MAP.get("TOOLRECALL_DOCS_COMPACT") == ("docs", "compact")
    assert ENV_MAP.get("TOOLRECALL_DOCS_COMPACT_RATIO") == ("docs", "compact_ratio")
    assert ENV_MAP.get("TOOLRECALL_DOCS_COMPACT_MIN_FTS_MB") == (
        "docs",
        "compact_min_fts_mb",
    )
    assert ENV_MAP.get("TOOLRECALL_DOCS_COMPACT_VACUUM") == ("docs", "compact_vacuum")

    empty = tmp_path / "empty.toml"
    empty.write_text("")
    monkeypatch.setenv("TOOLRECALL_CONFIG", str(empty))
    monkeypatch.setenv("TOOLRECALL_DOCS_COMPACT", "0")
    monkeypatch.setenv("TOOLRECALL_DOCS_COMPACT_RATIO", "2.5")
    monkeypatch.setenv("TOOLRECALL_DOCS_COMPACT_MIN_FTS_MB", "abc")  # invalid → ignored
    monkeypatch.setenv("TOOLRECALL_DOCS_COMPACT_VACUUM", "false")

    cfg = load_config()
    assert cfg.get("docs", "compact") is False
    assert cfg.get("docs", "compact_ratio") == 2.5
    assert "compact_min_fts_mb" not in (cfg._data.get("docs") or {})
    assert cfg.get("docs", "compact_vacuum") is False
