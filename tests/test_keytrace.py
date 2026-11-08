"""Unit tests for toolrecall.keytrace and the proxy's opt-in key trace."""

from __future__ import annotations

import json
import os
import tempfile
import unittest

from toolrecall import keytrace
from toolrecall.proxy import _key_trace


def _body(*msgs):
    return json.dumps(
        {"model": "m", "system": "s", "messages": list(msgs)}, sort_keys=True
    ).encode()


class TestKeyTrace(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = os.path.join(self.tmp.name, "trace")
        os.environ["TOOLRECALL_KEY_TRACE_DIR"] = self.dir

    def tearDown(self):
        os.environ.pop("TOOLRECALL_KEY_TRACE_DIR", None)
        self.tmp.cleanup()

    def test_off_by_default(self):
        os.environ.pop("TOOLRECALL_KEY_TRACE_DIR", None)
        _key_trace("claude-code", "h1", _body("a"), False)
        self.assertFalse(os.path.exists(self.dir))

    def test_explains_first_difference(self):
        u = {"role": "user", "content": "task"}
        ls1 = {"role": "user", "content": "drwxr-xr-x 2 u u 4096 Nov  7 10:19 src"}
        ls2 = {"role": "user", "content": "drwxr-xr-x 2 u u 4096 Nov  7 10:21 src"}
        _key_trace("claude-code", "h1", _body(u), False)
        _key_trace("claude-code", "h2", _body(u, ls1), False)
        _key_trace("claude-code", "h1", _body(u), True)  # rerun: call 1 replays
        _key_trace("claude-code", "h3", _body(u, ls2), False)  # breaks on the mtime
        rows = keytrace.explain(self.dir)
        self.assertEqual([r["hit"] for r in rows], [False, False, True, False])
        self.assertIn("first request", rows[0]["why"])
        self.assertEqual(rows[3]["path"], ".messages[1].content")
        self.assertIn("10:19", rows[3]["earlier"])
        self.assertIn("10:21", rows[3]["now"])
        text = keytrace.format_rows(rows)
        self.assertIn("1 replayed (HIT), 3 live (MISS)", text)

    def test_identical_body_miss(self):
        _key_trace("claude-code", "h1", _body({"role": "user", "content": "x"}), False)
        _key_trace("claude-code", "h1", _body({"role": "user", "content": "x"}), False)
        rows = keytrace.explain(self.dir)
        self.assertIn("expired or evicted", rows[1]["why"])

    def test_cli_missing_dir(self):
        self.assertEqual(keytrace.main([os.path.join(self.tmp.name, "nope")]), 1)


class TestCiEnv(unittest.TestCase):
    def test_ci_env_snippet(self):
        from toolrecall.adapters.claude_code import ci_env

        s = ci_env(8600)
        self.assertIn("ANTHROPIC_BASE_URL=http://127.0.0.1:8600", s)
        self.assertIn("X-ToolRecall-Canonicalize: claude-code", s)
        self.assertIn("PYTHONHASHSEED=0", s)
        self.assertIn("--strict-mcp-config", s)


if __name__ == "__main__":
    unittest.main()
