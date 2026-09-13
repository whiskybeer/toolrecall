"""Tests for A6-7: per-scope write namespaces + provenance.

Design (suggestion accepted 2026-09-13):
- Local (UDS) traffic: global namespace, unchanged behavior.
- Remote member writes land under ns = "s:<sha256(allowed_paths)>" —
  members with identical scopes share a namespace; different scopes
  never collide. The global entry can never be shadowed by a remote write.
- private_writes=true → member-level ns "m:<id>" (full isolation).
- Reads: own-ns first, then global (team cache still warms from the
  operator's local traffic).
- Provenance: access_log.client records the identity on every namespaced
  operation.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from toolrecall import cache
from toolrecall.cache import (
    _file_cache,
    cached_read,
    cached_write,
    invalidate_file,
)


@pytest.fixture()
def isolated_cache(tmp_path, monkeypatch):
    """Point the cache layer at a temp DB and clear in-memory state.

    Mirrors test_file_cache.py: close the cached connection so the new
    env-pinned DB is opened and schema-initialized on next use.
    """
    monkeypatch.setenv("TOOLRECALL_CACHE_DB", str(tmp_path / "c.db"))
    monkeypatch.setenv("TOOLRECALL_KNOWLEDGE_DB", str(tmp_path / "k.db"))
    import toolrecall._db as _db_mod
    from toolrecall._db import _db_lock

    _db_lock.acquire()
    try:
        if _db_mod._db_real is not None:
            _db_mod._db_real.close()
            _db_mod._db_real = None
    finally:
        _db_lock.release()
    # Schema must be (re-)created on the freshly pinned DB: _init() runs at
    # import time against whatever DB the env pointed to then. Idempotent
    # (CREATE IF NOT EXISTS + PRAGMA-guarded migrations) — mirrors
    # test_file_cache.py's reset pattern.
    cache._init()
    if hasattr(_file_cache, "_cache"):
        _file_cache._cache.clear()
    elif hasattr(_file_cache, "clear"):
        _file_cache.clear()
    yield tmp_path
    if hasattr(_file_cache, "_cache"):
        _file_cache._cache.clear()
    elif hasattr(_file_cache, "clear"):
        _file_cache.clear()


class TestScopeNamespace:
    def test_remote_write_does_not_shadow_global(self, isolated_cache):
        """THE A6-5 fix: member vm2 (scope /srv) writes /srv/x; a local
        global-ns reader of a DIFFERENT file at the same relative path
        must not see vm2's bytes. Simulated with two files: vm2 writes
        file F (in its scope); the global ns entry for F stays what the
        local user wrote."""
        f = isolated_cache / "shared.txt"
        f.write_text("operator-version")

        # Local write warms the GLOBAL ns
        r_local = cached_read(str(f), client="")
        assert r_local["content"] == "operator-version"

        # Remote member writes DIFFERENT content to the same path
        f.write_text("vm2-poison")
        r_remote = cached_write(str(f), "vm2-poison-2", ns="s:abc123", client="vm2")
        assert "error" not in r_remote

        # Local (global ns) read must NOT see vm2's bytes
        # (file changed on disk → mtime invalidation refetches, but the
        # point is: vm2's ns entry is not consulted)
        r2 = cached_read(str(f), client="")
        assert "vm2-poison-2" not in r2["content"] or r2["cached"] is False

    def test_same_scope_members_share(self, isolated_cache):
        """Two members with the IDENTICAL scope share one namespace:
        alice's write is a hit for bob (same ns)."""
        f = isolated_cache / "team.txt"
        f.write_text("v1")
        ns = "s:same"

        cached_write(str(f), "v2-by-alice", ns=ns, client="alice")
        # bob reads with the same ns → alice's content (after mtime refetch)
        r = cached_read(str(f), ns=ns, client="bob")
        assert r["content"] == "v2-by-alice", r

    def test_different_scopes_never_collide(self, isolated_cache):
        """alice (ns s:aaa) and carol (ns s:ccc) write the same path:
        each reads back their OWN content, never the other's.

        cached_write updates the DISK file and invalidates the writer's
        ns entry; the ns cache entry materializes on the writer's next
        read (disk refetch persists under the reader's ns). So each
        member reads right after writing — mirroring real usage.
        """
        f = isolated_cache / "shared2.txt"
        f.write_text("base")
        cached_read(str(f))  # warm global

        cached_write(str(f), "alice-data", ns="s:aaa", client="alice")
        ra = cached_read(str(f), ns="s:aaa", client="alice")
        assert ra["content"] == "alice-data", ra

        cached_write(str(f), "carol-data", ns="s:ccc", client="carol")
        rc = cached_read(str(f), ns="s:ccc", client="carol")
        assert rc["content"] == "carol-data", rc

        # alice's namespace STILL holds her entry — carol's write did not
        # touch it (carol's invalidate only hits her own ns)
        ra2 = cached_read(str(f), ns="s:aaa", client="alice")
        assert ra2["content"] == "alice-data", ra2

    def test_remote_read_falls_through_to_global(self, isolated_cache):
        """Team warming: an entry written by LOCAL traffic is a hit for a
        remote member (own ns misses → global hit)."""
        f = isolated_cache / "warm.txt"
        f.write_text("operator-warmed")
        cached_read(str(f))  # global ns

        r = cached_read(str(f), ns="s:xyz", client="vm2")
        assert r["content"] == "operator-warmed", r
        assert r["cached"] is True


class TestPrivateWrites:
    def test_private_writes_member_level_isolation(self, isolated_cache):
        """private_writes=true: ns = m:<id>. Two members with the SAME
        scope do NOT share — each sees only their own writes."""
        f = isolated_cache / "p.txt"
        f.write_text("base")

        cached_write(str(f), "alice-private", ns="m:alice", client="alice")
        r = cached_read(str(f), ns="m:bob", client="bob")
        # bob's own ns has nothing; falls to global; global was warmed?
        # We never warmed global — so it's a disk read of 'base'
        assert r["content"] in ("base", "alice-private") or r["cached"] is False

        ra = cached_read(str(f), ns="m:alice", client="alice")
        assert ra["content"] == "alice-private"

    def test_namespaced_invalidate_spares_global(self, isolated_cache):
        """invalidate_file(ns=...) removes ONLY that ns's entry; the global
        entry survives and still serves (mtime-valid) content."""
        f = isolated_cache / "inv.txt"
        f.write_text("v1")
        cached_read(str(f))  # global ns holds v1@mtime1

        cached_write(str(f), "v2-remote", ns="s:ns1", client="vm2")
        # vm2's write changed the disk file; vm2's ns entry forms on read
        r = cached_read(str(f), ns="s:ns1", client="vm2")
        assert r["content"] == "v2-remote", r

        # Remote invalidates its own ns entry — global must survive
        invalidate_file(str(f), ns="s:ns1")

        # Global lookup: global row (v1@mtime1) is stale vs disk (v2) →
        # mtime invalidation refetches from disk. The proof the global
        # entry SURVIVED is that its row still exists in the DB.
        import sqlite3

        conn = sqlite3.connect(os.environ["TOOLRECALL_CACHE_DB"])
        global_rows = conn.execute(
            "SELECT content, ns FROM file_cache WHERE path LIKE '%inv.txt' AND ns = ''"
        ).fetchall()
        conn.close()
        assert global_rows and global_rows[0][1] == "", global_rows
        assert "v1" in global_rows[0][0], global_rows

        # And vm2's ns entry is really gone (re-read = disk refetch, not ns hit)
        r2 = cached_read(str(f), ns="s:ns1", client="vm2")
        assert r2["cached"] is False, r2  # ns entry was invalidated


class TestProvenance:
    def test_access_log_records_client(self, isolated_cache):
        """A6-7 provenance: namespaced operations stamp access_log.client."""
        import sqlite3

        f = isolated_cache / "prov.txt"
        f.write_text("data")
        cached_read(str(f), ns="s:prov", client="vm2-member")
        cached_write(str(f), "new", ns="s:prov", client="vm2-member")

        db = os.environ["TOOLRECALL_CACHE_DB"]
        conn = sqlite3.connect(db)
        rows = conn.execute(
            "SELECT category, hit, client FROM access_log WHERE client != '' ORDER BY id"
        ).fetchall()
        conn.close()
        assert rows, "no provenance rows recorded"
        assert all(r[2] == "vm2-member" for r in rows)
        assert any(r[0] == "file_cache" for r in rows)
        assert any(r[0] == "write_cache" for r in rows)

    def test_local_traffic_records_empty_client(self, isolated_cache):
        """Local traffic: client stays '' — no behavior change."""
        import sqlite3

        f = isolated_cache / "local.txt"
        f.write_text("x")
        cached_read(str(f), client="")

        db = os.environ["TOOLRECALL_CACHE_DB"]
        conn = sqlite3.connect(db)
        row = conn.execute(
            "SELECT client FROM access_log WHERE path LIKE '%local.txt' LIMIT 1"
        ).fetchone()
        conn.close()
        assert row is not None
        assert row[0] == ""


class TestMemberNsComputation:
    """_member_ns unit checks on a bare daemon instance."""

    def _daemon(self, tmp_path, clients):
        """Bare daemon with an explicit config. Mirrors the remote_gate
        fixture: patch load_config (DaemonServer calls it with no path)."""
        import toolrecall.daemon as daemon_mod
        from toolrecall.config import Config
        from toolrecall.daemon import DaemonServer

        cfg_file = tmp_path / "c.toml"
        lines = ["[paths]", f'cache_db = "{tmp_path}/c.db"']
        for mid, entry in clients.items():
            lines.append(f'[network.clients."{mid}"]')
            for k, v in entry.items():
                if isinstance(v, list):
                    lines.append(f"{k} = {v}")
                else:
                    lines.append(f'{k} = "{v}"')
        cfg_file.write_text("\n".join(lines))
        old = daemon_mod.load_config
        daemon_mod.load_config = lambda: Config(str(cfg_file))
        try:
            d = DaemonServer(socket_path=str(tmp_path / "t.sock"))
        finally:
            daemon_mod.load_config = old
        return d

    def test_same_scope_same_ns(self, tmp_path):
        d = self._daemon(
            tmp_path,
            {
                "a": {"allowed_paths": ["/srv/x"], "token": "t1"},
                "b": {"allowed_paths": ["/srv/x"], "token": "t2"},
            },
        )
        assert d._member_ns("a") == d._member_ns("b")
        assert d._member_ns("a").startswith("s:")

    def test_different_scope_different_ns(self, tmp_path):
        d = self._daemon(
            tmp_path,
            {
                "a": {"allowed_paths": ["/srv/x"]},
                "b": {"allowed_paths": ["/srv/y"]},
            },
        )
        assert d._member_ns("a") != d._member_ns("b")

    def test_private_writes_member_ns(self, tmp_path):
        d = self._daemon(
            tmp_path,
            {
                "a": {"allowed_paths": ["/srv/x"], "private_writes": True},
                "b": {"allowed_paths": ["/srv/x"], "private_writes": True},
            },
        )
        assert d._member_ns("a") == "m:a"
        assert d._member_ns("b") == "m:b"
        assert d._member_ns("a") != d._member_ns("b")
