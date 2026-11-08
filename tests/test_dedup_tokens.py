"""Tests: MEASURED not-sent tokens (bridge dedup stubs).

The stub path is the only wire-verified CONTEXT-tier mechanism: content is
REPLACED by a note, so the tokens provably never reach the LLM. These tests
pin the counting chain: bridge stub event -> daemon dedup_tokens cmd ->
cache_stats mcp_dedup row -> get_stats exposure.
"""

import os
import unittest


class TestDedupTokens(unittest.TestCase):
    def setUp(self):
        # conftest's autouse fixture force-pins TOOLRECALL_CACHE_DB to a
        # per-test temp path — read it instead of assigning our own (a raw
        # os.environ set is overridden by the fixture and splits the test
        # from the DB get_stats() actually opens).
        from toolrecall.cache import _init

        _init()
        self.db_path = os.environ["TOOLRECALL_CACHE_DB"]

    def tearDown(self):
        pass

    def _mcp_dedup_row(self):
        import sqlite3

        con = sqlite3.connect(self.db_path)
        try:
            row = con.execute(
                "SELECT tokens_not_sent_measured FROM cache_stats WHERE category='mcp_dedup'"
            ).fetchone()
            return row[0] if row else 0
        finally:
            con.close()

    def _seed_file_cache(self):
        """get_stats() only lists categories with cache_stats rows — create
        the file_cache row with one real read of a temp file."""
        import tempfile as _tf

        from toolrecall.cache import cached_read

        fd, p = _tf.mkstemp(suffix=".py")
        os.close(fd)
        with open(p, "w") as f:
            f.write("seed = 1\n")
        cached_read(p, source="shim")
        return p

    def test_dedup_tokens_handler_counts(self):
        """Daemon cmd dedup_tokens lands in its own cache_stats category."""

        # Exercise the handler logic via the module-level _record it calls.
        from toolrecall.cache import _record

        _record("mcp_dedup", hit=False, path="/f.py (stub: ...)", tokens_not_sent_measured=1234)
        self.assertEqual(self._mcp_dedup_row(), 1234)
        # Accumulates
        _record("mcp_dedup", hit=False, path="/f.py", tokens_not_sent_measured=6)
        self.assertEqual(self._mcp_dedup_row(), 1240)

    def test_stub_does_not_pollute_disk_or_hit_counters(self):
        """A stub event must NOT touch hits/misses/disk-read counters."""
        from toolrecall.cache import _record, get_stats

        self._seed_file_cache()
        before = get_stats().get("file_cache", {})
        _record("mcp_dedup", hit=False, path="/x.py", tokens_not_sent_measured=500)
        after = get_stats()
        self.assertEqual(
            before.get("misses", 0), after["file_cache"]["misses"], "stub must not count as a miss"
        )
        self.assertEqual(
            before.get("tokens_served_from_disk", 0),
            after["file_cache"]["tokens_served_from_disk"],
            "stub must not touch the disk-read denominator",
        )
        # And it surfaces under its own category
        self.assertEqual(after["mcp_dedup"]["tokens_not_sent_measured"], 500)

    def test_stats_exposes_measured_per_category(self):
        """get_stats carries tokens_not_sent_measured in every category dict."""
        from toolrecall.cache import _record, get_stats

        self._seed_file_cache()
        _record("mcp_dedup", hit=False, path="/y.py", tokens_not_sent_measured=10)
        stats = get_stats()
        for cat in ("file_cache", "mcp_dedup"):
            self.assertIn("tokens_not_sent_measured", stats.get(cat, {}))


if __name__ == "__main__":
    unittest.main()
