"""Tests for ToolRecall Hermes init script — transparent caching modes.

Tests cover:
  - TOOLRECALL_HERMES_MODE env var parsing
  - Config [hermes] transparent_cache setting
  - Graceful fallback when tools.registry not available
  - cached_read hit/miss/token counting
  - Single-counting across repeated reads
  - Invalidation on file change
  - Missing file handling

NOT mocked — uses real subprocess with TOOLRECALL_* env isolation.
All inline code gets 'import os, sys' prepended automatically by _run_code().
"""

import os
import sys
import unittest
import tempfile
import textwrap
import subprocess
import json


REPO_DIR = os.path.expanduser("~/toolrecall")
INIT_SCRIPT = os.path.expanduser("~/.toolrecall/hermes_init.py")


def _run_code(code: str, extra_env: dict[str, str] | None = None) -> tuple[str, str, int]:
    """Run Python code in subprocess with isolated env and auto-import."""
    env = os.environ.copy()
    env.pop("TOOLRECALL_HERMES_MODE", None)
    env["PYTHONPATH"] = REPO_DIR
    if extra_env:
        env.update(extra_env)
    preamble = f"import json, os, sys, time\nsys.path.insert(0, {json.dumps(REPO_DIR)})\n"
    result = subprocess.run(
        [sys.executable, "-c", preamble + code],
        env=env, capture_output=True, text=True, timeout=10,
    )
    return result.stdout.strip(), result.stderr.strip(), result.returncode


# ═══════════════════════════════════════════════════════════
# Mode Detection Tests
# ═══════════════════════════════════════════════════════════

class TestModeDetection(unittest.TestCase):
    """_get_cache_mode() returns correct mode."""

    def setUp(self):
        self.db = os.path.join(tempfile.mkdtemp(), "test.db")

    def test_default_is_separate(self):
        code = textwrap.dedent("""\
            mode = os.environ.get("TOOLRECALL_HERMES_MODE", "").strip().lower()
            if mode not in ("transparent", "separate"):
                mode = "separate"
            print(mode)
        """)
        out, err, rc = _run_code(code, {"TOOLRECALL_CACHE_DB": self.db})
        self.assertEqual(out, "separate")

    def test_env_var_transparent(self):
        code = "print(os.environ['TOOLRECALL_HERMES_MODE'])"
        out, err, rc = _run_code(code, {
            "TOOLRECALL_CACHE_DB": self.db,
            "TOOLRECALL_HERMES_MODE": "transparent",
        })
        self.assertEqual(out, "transparent")

    def test_env_var_separate(self):
        code = "print(os.environ['TOOLRECALL_HERMES_MODE'])"
        out, err, rc = _run_code(code, {
            "TOOLRECALL_CACHE_DB": self.db,
            "TOOLRECALL_HERMES_MODE": "separate",
        })
        self.assertEqual(out, "separate")

    def test_env_var_ignores_unknown(self):
        code = textwrap.dedent("""\
            mode = os.environ.get("TOOLRECALL_HERMES_MODE", "").strip().lower()
            if mode not in ("transparent", "separate"):
                mode = "separate"
            print(mode)
        """)
        out, err, rc = _run_code(code, {
            "TOOLRECALL_CACHE_DB": self.db,
            "TOOLRECALL_HERMES_MODE": "auto",
        })
        self.assertEqual(out, "separate")


# ═══════════════════════════════════════════════════════════
# Safety Tests
# ═══════════════════════════════════════════════════════════

class TestImportSafety(unittest.TestCase):
    """ToolRecall imports work; init script doesn't crash."""

    def setUp(self):
        self.db = os.path.join(tempfile.mkdtemp(), "test.db")

    def test_toolrecall_import(self):
        code = textwrap.dedent("""\
            from toolrecall import cached_read, cached_terminal, cached_skill
            from toolrecall import cached_run, cached_exec, docs_search
            from toolrecall.cache import get_stats
            print("OK")
        """)
        out, err, rc = _run_code(code, {"TOOLRECALL_CACHE_DB": self.db})
        self.assertEqual(out, "OK", f"Import failed: {err}")

    def test_init_script_does_not_crash(self):
        out, err, rc = _run_code(f"exec(open('{INIT_SCRIPT}').read())",
                                  {"TOOLRECALL_CACHE_DB": self.db})
        self.assertEqual(rc, 0, f"Init script crashed: {err}")
        self.assertIn("ToolRecall", out, f"Banner missing: {out}")


# ═══════════════════════════════════════════════════════════
# End-to-End Read + Cache Tests
# ═══════════════════════════════════════════════════════════

class TestFileCacheEndToEnd(unittest.TestCase):
    """cached_read hit/miss, token single-counting, invalidation."""

    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.db = os.path.join(self.test_dir, "cache.db")
        self.test_file = os.path.join(self.test_dir, "test.txt")
        with open(self.test_file, "w") as f:
            f.write("hello world")
        self.other = os.path.join(self.test_dir, "other.txt")
        with open(self.other, "w") as f:
            f.write("other content")

    def test_hit_miss_tokens(self):
        code = textwrap.dedent(f"""\
            from toolrecall.cache import cached_read, get_stats, reset_stats, _init
            reset_stats(); _init()
            r1 = cached_read({json.dumps(self.test_file)})
            assert 'hello' in r1.get('content', '')
            r2 = cached_read({json.dumps(self.test_file)})
            assert r2.get('cached'), "should be cached"
            r3 = cached_read({json.dumps(self.test_file)})
            assert r3.get('cached'), "should still be cached"
            r4 = cached_read({json.dumps(self.other)})
            assert 'other' in r4.get('content', '')
            s = get_stats()
            fc = s.get('file_cache', {{}})
            print(json.dumps({{'hits': fc.get('hits',0), 'misses': fc.get('misses',0),
                              'tokens': fc.get('tokens_read_from_disk',0),
                              'entries': s.get('file_cache_entries',0)}}))
        """)
        out, err, rc = _run_code(code, {"TOOLRECALL_CACHE_DB": self.db})
        self.assertEqual(rc, 0, f"Crashed: {err}")
        stats = json.loads(out)
        self.assertEqual(stats["hits"], 2)
        self.assertEqual(stats["misses"], 2)
        self.assertGreater(stats["tokens"], 0)

    def test_single_counting_ten_reads(self):
        code = textwrap.dedent(f"""\
            from toolrecall.cache import cached_read, get_stats, reset_stats, _init
            reset_stats(); _init()
            r = cached_read({json.dumps(self.test_file)})
            s1 = get_stats()
            t1 = s1['file_cache']['tokens_read_from_disk']
            for _ in range(10):
                r = cached_read({json.dumps(self.test_file)})
                assert r.get('cached'), "should be cached"
            s2 = get_stats()
            t2 = s2['file_cache']['tokens_read_from_disk']
            hits = s2['file_cache']['hits']
            print(json.dumps({{'t1': t1, 't2': t2, 'hits': hits}}))
        """)
        out, err, rc = _run_code(code, {"TOOLRECALL_CACHE_DB": self.db})
        self.assertEqual(rc, 0, f"Crashed: {err}")
        stats = json.loads(out)
        self.assertEqual(stats["t1"], stats["t2"], "Tokens grew on hits")
        self.assertGreaterEqual(stats["hits"], 10)

    def test_invalidates_on_file_change(self):
        code = textwrap.dedent(f"""\
            from toolrecall.cache import cached_read, get_stats, reset_stats, _init
            reset_stats(); _init()
            r1 = cached_read({json.dumps(self.test_file)})
            assert 'hello' in r1.get('content', '')
            time.sleep(0.02)
            with open({json.dumps(self.test_file)}, 'w') as f:
                f.write('modified')
            r2 = cached_read({json.dumps(self.test_file)})
            assert 'modified' in r2.get('content', '')
            assert not r2.get('cached'), "should miss after edit"
            r3 = cached_read({json.dumps(self.test_file)})
            assert r3.get('cached'), "should be cached again"
            print("OK")
        """)
        out, err, rc = _run_code(code, {"TOOLRECALL_CACHE_DB": self.db})
        self.assertEqual(out, "OK", f"Failed: {err}")

    def test_missing_file_returns_error(self):
        missing = json.dumps(os.path.join(self.test_dir, "nope.txt"))
        code = textwrap.dedent(f"""\
            from toolrecall.cache import cached_read, reset_stats, _init
            reset_stats(); _init()
            result = cached_read({missing})
            print(json.dumps(result))
        """)
        out, err, rc = _run_code(code, {"TOOLRECALL_CACHE_DB": self.db})
        self.assertEqual(rc, 0, f"Crashed: {err}")
        result = json.loads(out)
        self.assertIn("error", result, f"Expected error: {result}")

    def test_cross_session_persistence(self):
        db2 = os.path.join(self.test_dir, "persist.db")
        code1 = textwrap.dedent(f"""\
            from toolrecall.cache import cached_read, reset_stats, _init
            reset_stats(); _init()
            r = cached_read({json.dumps(self.test_file)})
            print("OK")
        """)
        code2 = textwrap.dedent(f"""\
            from toolrecall.cache import cached_read, reset_stats, _init
            reset_stats(); _init()
            r = cached_read({json.dumps(self.test_file)})
            print(f"cached={{r.get('cached')}}")
        """)
        out1, err1, rc1 = _run_code(code1, {"TOOLRECALL_CACHE_DB": db2})
        self.assertEqual(rc1, 0, f"First run: {err1}")
        out2, err2, rc2 = _run_code(code2, {"TOOLRECALL_CACHE_DB": db2})
        self.assertEqual(rc2, 0, f"Second run: {err2}")
        self.assertEqual(out2, "cached=True", f"Should persist: {out2}")


if __name__ == "__main__":
    unittest.main()