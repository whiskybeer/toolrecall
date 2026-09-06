"""Integration tests: [cache].file_ttls wired into cached_read().

resolve_file_ttl() itself is unit-tested in test_ttl_policy.py. These tests
prove the WIRING: trust-window hits serve without mtime validation, expired
windows re-read, ttl=0 paths are serve-through, and folder/filetype globs
work end-to-end through the real cache layer.
"""

import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

test_db_dir = tempfile.mkdtemp()
os.environ["TOOLRECALL_CACHE_DB"] = os.path.join(test_db_dir, "test_fttl.db")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from toolrecall import cache as cache_mod  # noqa: E402


class _FileTtlEnv(unittest.TestCase):
    """Hermetic per-test config + temp tree."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tree = self._tmp.name
        # Isolate the in-memory LRU between tests
        cache_mod._file_cache.clear()
        # Fresh DB state per test (module-level DB is shared across cases)
        with cache_mod._db() as conn:
            conn.execute("DELETE FROM file_cache")
        # Per-test [cache] section — cached_read resolves via `config.get("cache")`
        self._orig_get = cache_mod.config.get

        def fake_get(*keys, default=None):
            if keys and keys[0] == "cache":
                return self.cache_cfg
            return self._orig_get(*keys, default=default)

        cache_mod.config.get = fake_get
        self.addCleanup(setattr, cache_mod.config, "get", self._orig_get)

        self.cache_cfg = {}

    def _write(self, rel, content):
        p = os.path.join(self.tree, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write(content)
        return p

    def _db_rows(self):
        with cache_mod._db() as conn:
            return conn.execute("SELECT path, content, cached_at FROM file_cache").fetchall()


class TestTrustWindow(_FileTtlEnv):
    def test_fresh_read_then_trust_hit_within_window(self):
        p = self._write("doc.md", "v1")
        r1 = cache_mod.cached_read(p)
        self.assertFalse(r1["cached"])

        # Simulate a change WITHOUT updating mtime is impossible portably —
        # instead prove the trust window serves even when mtime WOULD have
        # changed: bump mtime forward, content too.
        future = time.time() + 9999
        os.utime(p, (future, future))
        with open(p, "w") as f:
            f.write("v2-changed")

        self.cache_cfg = {"file_ttls": {"**/*.md": 600}}
        r2 = cache_mod.cached_read(p)
        self.assertTrue(r2["cached"])
        self.assertEqual(r2["content"], "v1", "trust window must serve stale content")

    def test_expired_window_re_reads(self):
        p = self._write("doc.md", "v1")
        cache_mod.cached_read(p)

        # Backdate the cached_at beyond the window
        with cache_mod._db() as conn:
            conn.execute(
                "UPDATE file_cache SET cached_at = ? WHERE path = ?", (time.time() - 700, p)
            )
        cache_mod._file_cache.clear()

        # mtime unchanged (so the -1 default path would still be a hit) —
        # but the window expired: trust-window logic must re-read.
        # NOTE: with an expired window and unchanged mtime, the re-read
        # lands in the mtime-match branch → served as a normal hit. The
        # observable difference is only for CHANGED files:
        os.utime(p, (time.time() + 5, time.time() + 5))
        with open(p, "w") as f:
            f.write("v2")
        self.cache_cfg = {"file_ttls": {"**/*.md": 600}}
        r = cache_mod.cached_read(p)
        self.assertEqual(r["content"], "v2", "expired window must NOT serve stale content")

    def test_no_window_mtime_change_is_miss(self):
        p = self._write("doc.md", "v1")
        cache_mod.cached_read(p)
        stored = cache_mod._file_cache.get(p)["mtime"]
        with open(p, "w") as f:
            f.write("v2")
        # Force a mtime strictly newer than the stored entry (the write's own
        # mtime can tie with `stored` on same-tick clock granularity).
        os.utime(p, (stored + 10, stored + 10))
        final = os.stat(p).st_mtime
        self.assertGreater(final, stored, "rewrite must produce a newer mtime")
        self.cache_cfg = {}  # default: no file_ttls → -1 → mtime always
        r = cache_mod.cached_read(p)
        self.assertEqual(r["content"], "v2")


class TestNeverCache(_FileTtlEnv):
    def test_ttl_zero_is_serve_through(self):
        p = self._write("secret.env", "KEY=1")
        self.cache_cfg = {"file_ttls": {"**/*.env": 0}}
        for _ in range(3):
            r = cache_mod.cached_read(p)
            self.assertFalse(r["cached"], "ttl=0 must never serve from cache")
            self.assertEqual(r["content"], "KEY=1")
        self.assertEqual(self._db_rows(), [], "ttl=0 must not persist to SQLite")
        self.assertEqual(len(cache_mod._file_cache), 0, "ttl=0 must not enter memory LRU")

    def test_ttl_zero_still_picks_up_changes(self):
        p = self._write("x.log", "line1")
        self.cache_cfg = {"file_ttls": {p: 0}}
        self.assertEqual(cache_mod.cached_read(p)["content"], "line1")
        with open(p, "w") as f:
            f.write("line2")
        self.assertEqual(cache_mod.cached_read(p)["content"], "line2")


class TestGlobs(_FileTtlEnv):
    def test_folder_glob_matches_subtree(self):
        p = self._write("vault/note.md", "note")
        self.cache_cfg = {"file_ttls": {os.path.join(self.tree, "vault", "**"): 300}}
        r1 = cache_mod.cached_read(p)
        self.assertFalse(r1["cached"])
        r2 = cache_mod.cached_read(p)
        self.assertTrue(r2["cached"], "second read within window should hit (memory LRU)")

    def test_filetype_glob(self):
        p1 = self._write("a.py", "code")
        p2 = self._write("b.md", "prose")
        # .py → trust window (persists); .md → never-cache (serve-through)
        self.cache_cfg = {"file_ttls": {"**/*.py": 3600, "**/*.md": 0}}
        cache_mod.cached_read(p1)
        cache_mod.cached_read(p2)
        rows = {r[0] for r in self._db_rows()}
        self.assertEqual(rows, {p1}, "only the trust-windowed .py may persist; ttl=0 .md must not")
        # .md served fresh, .py served from cache on 2nd read
        self.assertFalse(cache_mod.cached_read(p2)["cached"])
        self.assertTrue(cache_mod.cached_read(p1)["cached"])

    def test_exact_beats_glob_beats_global(self):
        pa = self._write("special.py", "a")
        pb = self._write("other.py", "b")
        self.cache_cfg = {
            "file_ttls": {pa: 0, "**/*.py": 3600},
            "file_ttl": -1,
        }
        cache_mod.cached_read(pa)
        cache_mod.cached_read(pb)
        rows = {r[0] for r in self._db_rows()}
        self.assertEqual(rows, {pb}, "exact ttl=0 must win over glob; glob must win over global")


if __name__ == "__main__":
    unittest.main()
