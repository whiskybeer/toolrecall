"""Tests for FTS index freshness: last_index stamping, TTL staleness check,
background auto-refresh on docs_search, and the safety guards.

Isolation: every test points TOOLRECALL_KNOWLEDGE_DB at a tmp_path DB and
stubs docs._get_config() so the real ~/.config/toolrecall/toolrecall.toml
(and its scan_dirs) can never leak into a test — otherwise a background
index_all() would walk the real home directory and hang the suite.
"""

from __future__ import annotations

import os
import threading
import time

import pytest

import toolrecall.docs as docs


class _Unset:
    pass


_UNSET = _Unset()


class FakeConfig:
    """Minimal Config stand-in: only the keys docs.py actually reads.

    Keys left as _UNSET fall through to the caller's `default=` argument,
    matching real Config.get semantics.
    """

    def __init__(self):
        self.data = {
            ("docs", "index_ttl"): 86400.0,
            ("sources", "scan_dirs"): _UNSET,  # _UNSET = unconfigured
            ("sources", "knowledge"): _UNSET,
            ("sources", "memory"): _UNSET,
            ("sources", "scan_extensions"): _UNSET,
            ("sources", "scan_ignore"): _UNSET,
        }

    def get(self, section, key, default=None):
        val = self.data.get((section, key), _UNSET)
        if val is _UNSET:
            return default
        return val

    @property
    def agent_home(self) -> str:
        """Mirror real Config.agent_home: AGENT_HOME env → deterministic test default."""
        env = os.environ.get("AGENT_HOME") or os.environ.get("TOOLRECALL_AGENT_HOME")
        if env:
            return os.path.expanduser(env)
        return "/nonexistent-agent-home"  # not $HOME, not ~/.hermes — deterministic


@pytest.fixture()
def fresh_db(tmp_path, monkeypatch):
    """Isolated knowledge DB + stubbed config + pristine refresh state."""
    db = tmp_path / "knowledge.db"
    monkeypatch.setenv("TOOLRECALL_KNOWLEDGE_DB", str(db))

    import toolrecall.docs as docs

    fake = FakeConfig()
    monkeypatch.setattr(docs, "_get_config", lambda: fake)
    docs._refreshing = False
    yield docs, db, fake
    docs._refreshing = False


def _seed_page(docs, source="t", path="p.md", content="hello world content"):
    conn = docs._get_db()
    docs._ensure_tables(conn)
    conn.execute(
        "INSERT OR REPLACE INTO pages (source, path, title, content, url) VALUES (?,?,?,?,?)",
        (source, path, path, content, "file://x"),
    )
    conn.commit()
    conn.close()


def _make_stale(docs, hours=2.0):
    conn = docs._get_db()
    conn.execute(
        "INSERT OR REPLACE INTO index_meta (key, value) VALUES ('last_index', ?)",
        (str(time.time() - hours * 3600),),
    )
    conn.commit()
    conn.close()


def _wait_refresh_done(docs, timeout_s=5.0):
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if not docs._refreshing:
            return True
        time.sleep(0.05)
    return False


# ── Stamping ────────────────────────────────────────────────────────────────


def test_last_index_time_none_when_db_missing(fresh_db):
    docs, db, _ = fresh_db
    assert docs.get_last_index_time() is None


def test_index_directory_stamps_last_index(fresh_db, tmp_path):
    docs, db, _ = fresh_db
    src = tmp_path / "vault"
    src.mkdir()
    (src / "note.md").write_text("# Note\n\nobsidian content")

    assert docs.get_last_index_time() is None
    docs.index_directory(str(src), source="vault")
    stamped = docs.get_last_index_time()
    assert stamped is not None
    assert abs(stamped - time.time()) < 60


def test_index_all_stamps_last_index(fresh_db, tmp_path):
    docs, db, fake = fresh_db
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.md").write_text("# Doc A\n\nsome content about caching")
    fake.data[("sources", "scan_dirs")] = [str(src)]

    assert docs.get_last_index_time() is None
    docs.index_all()
    stamped = docs.get_last_index_time()
    assert stamped is not None
    assert abs(stamped - time.time()) < 60


# ── TTL ─────────────────────────────────────────────────────────────────────


def test_ttl_default_is_daily(fresh_db):
    docs, db, _ = fresh_db
    assert docs.get_index_ttl() == 86400.0


def test_ttl_from_config(fresh_db):
    docs, db, fake = fresh_db
    fake.data[("docs", "index_ttl")] = 60
    assert docs.get_index_ttl() == 60.0


def test_ttl_zero_disables(fresh_db):
    docs, db, fake = fresh_db
    fake.data[("docs", "index_ttl")] = 0
    assert docs.get_index_ttl() == 0.0


def test_ttl_garbage_falls_back_to_default(fresh_db):
    docs, db, fake = fresh_db
    fake.data[("docs", "index_ttl")] = "not-a-number"
    assert docs.get_index_ttl() == 86400.0


# ── Source guard ────────────────────────────────────────────────────────────


def test_default_scan_dirs_are_curated_not_home():
    """The built-in default must never be (or contain) $HOME itself.

    Indexing all of $HOME was the original default and produced a multi-GB
    junk-filled DB. The default is now the curated memory/skills set.
    """
    home = str(docs.Path.home()) if hasattr(docs, "Path") else None
    for d in docs._default_scan_dirs():
        expanded = os.path.expanduser(d)
        assert expanded != home, f"default scan dir must not be $HOME: {d}"


def test_default_scan_dirs_follow_agent_home(fresh_db, monkeypatch):
    """AGENT_HOME drives the curated default — agent-agnostic, not Hermes-only.

    Any agent that sets AGENT_HOME gets its own memories/skills indexed by
    default; ~/.hermes is only the unconfigured fallback, never a hardcode.
    """
    docs, db, fake = fresh_db
    agent_home = str(db.parent / "other-agent")
    monkeypatch.setenv("AGENT_HOME", agent_home)
    dirs = docs._default_scan_dirs()
    assert dirs == [
        os.path.join(agent_home, "memories"),
        os.path.join(agent_home, "skills"),
    ]


def test_sources_unconfigured_means_default_sources(fresh_db):
    """Unconfigured → curated default (auto-refresh allowed, bounded walk)."""
    docs, db, fake = fresh_db
    fake.data[("sources", "scan_dirs")] = None
    fake.data[("sources", "knowledge")] = []
    fake.data[("sources", "memory")] = {}
    # Sources resolve to the curated default → auto-refresh fires
    assert docs._auto_refresh_sources_configured() is True


def test_empty_scan_dirs_disables_auto_refresh(fresh_db):
    """Explicitly empty scan_dirs + no knowledge/memory → nothing to index."""
    docs, db, fake = fresh_db
    fake.data[("sources", "scan_dirs")] = []
    fake.data[("sources", "knowledge")] = []
    fake.data[("sources", "memory")] = {}
    assert docs._auto_refresh_sources_configured() is False


def test_sources_configured_enables_auto_refresh(fresh_db, tmp_path):
    docs, db, fake = fresh_db
    fake.data[("sources", "scan_dirs")] = [str(tmp_path)]
    assert docs._auto_refresh_sources_configured() is True


def test_unconfigured_sources_never_spawns_worker(fresh_db):
    """Regression guard: sources resolving to empty → no worker, ever.

    (With the curated default, *unconfigured* now means default sources —
    bounded and safe. Only an explicitly empty source set disables refresh.)
    """
    docs, db, fake = fresh_db
    fake.data[("docs", "index_ttl")] = 1  # maximally stale
    fake.data[("sources", "scan_dirs")] = []  # explicitly empty → nothing to index
    fake.data[("sources", "knowledge")] = []
    fake.data[("sources", "memory")] = {}
    _seed_page(docs, content="regression probe unique_token_xyz")

    threads_before = threading.active_count()
    docs._maybe_refresh_index_async()
    time.sleep(0.1)
    assert threading.active_count() == threads_before
    assert not docs._refreshing

    out = docs.docs_search("unique_token_xyz")
    assert "unique_token_xyz" in out
    assert threading.active_count() == threads_before


# ── Refresh decision ────────────────────────────────────────────────────────


def test_refresh_fires_when_configured_and_stale(fresh_db, tmp_path):
    docs, db, fake = fresh_db
    src = tmp_path / "src"
    src.mkdir()
    (src / "new.md").write_text("# Fresh\n\nbrand new doc")
    fake.data[("sources", "scan_dirs")] = [str(src)]
    fake.data[("docs", "index_ttl")] = 3600

    docs.index_all()
    _make_stale(docs)

    docs._maybe_refresh_index_async()
    assert _wait_refresh_done(docs)
    assert docs.get_last_index_time() > time.time() - 60


def test_no_refresh_when_index_is_fresh(fresh_db, tmp_path):
    docs, db, fake = fresh_db
    src = tmp_path / "src"
    src.mkdir()
    fake.data[("sources", "scan_dirs")] = [str(src)]
    fake.data[("docs", "index_ttl")] = 3600

    docs.index_all()  # stamps now → fresh
    threads_before = threading.active_count()
    docs._maybe_refresh_index_async()
    time.sleep(0.1)
    assert threading.active_count() == threads_before


def test_single_flight_no_second_worker(fresh_db, tmp_path):
    docs, db, fake = fresh_db
    src = tmp_path / "src"
    src.mkdir()
    fake.data[("sources", "scan_dirs")] = [str(src)]
    fake.data[("docs", "index_ttl")] = 3600

    docs.index_all()
    _make_stale(docs)

    docs._refreshing = True  # simulate refresh already in flight
    docs._maybe_refresh_index_async()
    time.sleep(0.1)
    # No additional worker spawned beyond the simulated one.
    assert docs._refreshing is True  # untouched by the second call


# ── End-to-end: docs_search serves stale index, refresh improves next query ─


def test_docs_search_serves_stale_and_refreshes_background(fresh_db, tmp_path):
    docs, db, fake = fresh_db
    src = tmp_path / "src"
    src.mkdir()
    (src / "x.md").write_text("# X\n\nsearchable zebra content")
    fake.data[("sources", "scan_dirs")] = [str(src)]
    fake.data[("docs", "index_ttl")] = 3600

    docs.index_all()
    _make_stale(docs)

    out = docs.docs_search("zebra")
    assert "zebra" in out.lower() or "Found" in out  # served from stale index
    assert _wait_refresh_done(docs)  # background refresh completes
    assert docs.get_last_index_time() > time.time() - 60


def test_ttl_zero_docs_search_never_refreshes(fresh_db, tmp_path):
    docs, db, fake = fresh_db
    _seed_page(docs, content="probe ttl zero qqq")
    fake.data[("docs", "index_ttl")] = 0

    threads_before = threading.active_count()
    docs.docs_search("qqq")
    time.sleep(0.1)
    assert threading.active_count() == threads_before
    assert not docs._refreshing
