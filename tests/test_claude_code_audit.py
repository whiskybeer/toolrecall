"""Unit tests for toolrecall.adapters.claude_code_audit."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from toolrecall.adapters import claude_code_audit as audit


def _write_session(proj: Path, slug: str, lines: list) -> Path:
    d = proj / slug
    d.mkdir(parents=True, exist_ok=True)
    p = d / "s1.jsonl"
    p.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")
    return p


def _call(uid, mid, i, cw, cr, o):
    return {
        "type": "assistant",
        "uuid": uid,
        "message": {
            "id": mid,
            "usage": {
                "input_tokens": i,
                "cache_creation_input_tokens": cw,
                "cache_read_input_tokens": cr,
                "output_tokens": o,
            },
        },
    }


class TestClaudeCodeAudit(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.proj = Path(self.tmp.name)
        instr = {
            "type": "attachment",
            "uuid": "a1",
            "attachment": {
                "type": "instructions",
                "files": [{"path": "/w/AGENTS.md", "content": "x" * 4000}],
            },
        }
        c1 = _call("u1", "m1", 0, 10000, 0, 100)
        c2 = _call("u2", "m2", 0, 0, 10000, 100)
        self.lines = [instr, c1, c2, c2]  # c2 duplicated: transcript resume artifact

    def tearDown(self):
        self.tmp.cleanup()

    def test_session_numbers(self):
        p = _write_session(self.proj, "-w", self.lines)
        s = audit.analyse_session(str(p))
        self.assertEqual(s["calls"], 2)  # duplicate line ignored
        self.assertEqual(s["baseline_tokens"], 10000)
        # 10000*1.25 + 100*5 + 10000*0.1 + 100*5
        self.assertAlmostEqual(s["cost_units"], 14500.0)
        part = s["parts"]["instructions: /w/AGENTS.md"]
        self.assertEqual(part["tokens_est"], 1000)
        self.assertAlmostEqual(part["cost_units"], 1000 * (1.25 + 0.1))

    def test_audit_skips_tmp_workdirs_by_default(self):
        _write_session(self.proj, "-w", self.lines)
        _write_session(self.proj, "-tmp-bench", self.lines)
        self.assertEqual(audit.audit(str(self.proj))["sessions"], 1)
        self.assertEqual(audit.audit(str(self.proj), include_tmp=True)["sessions"], 2)

    def test_report_and_shares(self):
        _write_session(self.proj, "-w", self.lines)
        r = audit.audit(str(self.proj))
        self.assertAlmostEqual(r["baseline_share"], 10000 * 1.35 / 14500)
        self.assertEqual(r["parts"][0]["name"], "instructions: /w/AGENTS.md")
        text = audit.format_report(r)
        self.assertIn("Fixed starting context", text)
        self.assertIn("/w/AGENTS.md", text)

    def test_empty(self):
        self.assertIn("No Claude Code sessions", audit.format_report(audit.audit(str(self.proj))))


if __name__ == "__main__":
    unittest.main()
