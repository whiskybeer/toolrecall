"""
AST Structural Validation Tests — SecurityGate.check_ast_injection()

Tests cover:
  - Blocking exec(), eval(), compile(), __import__() calls
  - Blocking import statements and function definitions
  - Allowing legitimate argument values (JSON, natural language, short strings)
  - Skipping non-string values (int, list, dict)
  - Skipping short strings (<10 chars)
  - Performance: <0.1ms per call
  - Integration through _handle_mcp_call() pathway

Usage:
  python3 -m pytest tests/test_ast_security.py -v --tb=short
"""

import os
import sys
import time
import unittest
import tempfile

# Isolated test environment
test_dir = tempfile.mkdtemp()
os.environ["TOOLRECALL_CACHE_DB"] = os.path.join(test_dir, "test_ast.db")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from toolrecall.daemon import SecurityGate
from toolrecall.config import Config
from unittest.mock import MagicMock


def _log(msg: str) -> None:
    print(f"  [AST] {msg}", file=sys.stderr, flush=True)


class TestASTStructuralValidation(unittest.TestCase):
    """Batch 1: AST Structural Validation Gate."""

    @classmethod
    def setUpClass(cls):
        _log("=== AST Structural Validation Tests ===")

    def setUp(self):
        self.mock_cfg = MagicMock(spec=Config)
        self.mock_cfg.mcp_ast_check_enabled = True
        self.mock_cfg.mcp_tool_access_control = False
        self.mock_cfg.mcp_allowed_paths = []
        self.mock_cfg.mcp_allow_terminal = False
        self.mock_cfg.mcp_allowed_terminal_commands = []
        self.mock_cfg.mcp_allow_invalidate = False
        self.mock_cfg.mcp_multiplex_enabled = True
        self.mock_cfg.mcp_multiplex_servers = []

    def test_ast_blocks_exec_call(self):
        """AST-001: Block exec() in tool arguments."""
        _log("Testing exec() blocking")
        security = SecurityGate(self.mock_cfg)
        err = security.check_ast_injection({"code": "exec('print(1)')"})
        self.assertIsNotNone(err)
        self.assertIn("exec", err)
        _log("  PASS: exec() blocked")

    def test_ast_blocks_eval_call(self):
        """AST-002: Block eval() in tool arguments."""
        _log("Testing eval() blocking")
        security = SecurityGate(self.mock_cfg)
        err = security.check_ast_injection({"expr": "eval('__import__(\"os\")')"})
        self.assertIsNotNone(err)
        self.assertIn("eval", err)
        _log("  PASS: eval() blocked")

    def test_ast_blocks_compile_call(self):
        """AST-003: Block compile() in tool arguments."""
        _log("Testing compile() blocking")
        security = SecurityGate(self.mock_cfg)
        err = security.check_ast_injection({"code": "compile('print(1)', '<string>', 'exec')"})
        self.assertIsNotNone(err)
        self.assertIn("compile", err)
        _log("  PASS: compile() blocked")

    def test_ast_blocks_import_statement(self):
        """AST-004: Block import statements in tool arguments."""
        _log("Testing import statement blocking")
        security = SecurityGate(self.mock_cfg)
        err = security.check_ast_injection({"code": "import os; os.system('rm -rf /')"})
        self.assertIsNotNone(err)
        self.assertIn("import statement", err)
        _log("  PASS: import statement blocked")

    def test_ast_blocks_from_import_statement(self):
        """AST-005: Block 'from x import y' in tool arguments."""
        _log("Testing from-import blocking")
        security = SecurityGate(self.mock_cfg)
        err = security.check_ast_injection({"code": "from os import system"})
        self.assertIsNotNone(err)
        self.assertIn("import statement", err)
        _log("  PASS: from import blocked")

    def test_ast_blocks_function_def(self):
        """AST-006: Block function definition in tool arguments."""
        _log("Testing function def blocking")
        security = SecurityGate(self.mock_cfg)
        err = security.check_ast_injection({"code": "def malicious():\n    exec('rm -rf')"})
        self.assertIsNotNone(err)
        self.assertIn("function definition", err)
        _log("  PASS: function definition blocked")

    def test_ast_blocks_async_function_def(self):
        """AST-007: Block async function definition in tool arguments."""
        _log("Testing async function def blocking")
        security = SecurityGate(self.mock_cfg)
        err = security.check_ast_injection({"code": "async def steal():\n    pass"})
        self.assertIsNotNone(err)
        self.assertIn("function definition", err)
        _log("  PASS: async function definition blocked")

    def test_ast_blocks_dunder_import(self):
        """AST-008: Block __import__() in tool arguments."""
        _log("Testing __import__() blocking")
        security = SecurityGate(self.mock_cfg)
        err = security.check_ast_injection({"code": "__import__('os').system('id')"})
        self.assertIsNotNone(err)
        self.assertIn("__import__", err)
        _log("  PASS: __import__() blocked")

    # ─── Legitimate values must pass ─────────────────

    def test_ast_allows_json_literal(self):
        """AST-009: JSON literal strings pass through."""
        _log("Testing JSON literal pass-through")
        security = SecurityGate(self.mock_cfg)
        err = security.check_ast_injection({"content": '{"key": "value", "nested": [1, 2, 3]}'})
        self.assertIsNone(err)
        _log("  PASS: JSON literal allowed")

    def test_ast_allows_natural_language(self):
        """AST-010: Natural language text passes through."""
        _log("Testing natural language pass-through")
        security = SecurityGate(self.mock_cfg)
        err = security.check_ast_injection({
            "description": "Please write a function that calculates fibonacci numbers."
        })
        self.assertIsNone(err)
        _log("  PASS: Natural language allowed")

    def test_ast_allows_code_snippet_in_markdown(self):
        """AST-011: Code snippets in markdown pass through (single line, not parseable as module)."""
        _log("Testing markdown code snippet")
        security = SecurityGate(self.mock_cfg)
        # A single expression like `x + 1` is valid Python but harmless
        err = security.check_ast_injection({
            "comment": "Use `x + 1` to increment the counter"
        })
        self.assertIsNone(err)
        _log("  PASS: Markdown code snippet allowed")

    def test_ast_allows_html_content(self):
        """AST-012: HTML content passes through."""
        _log("Testing HTML pass-through")
        security = SecurityGate(self.mock_cfg)
        err = security.check_ast_injection({
            "html": "<div class='container'><p>Hello world</p></div>"
        })
        self.assertIsNone(err)
        _log("  PASS: HTML content allowed")

    def test_ast_allows_urls(self):
        """AST-013: URLs pass through."""
        _log("Testing URL pass-through")
        security = SecurityGate(self.mock_cfg)
        err = security.check_ast_injection({
            "url": "https://github.com/whiskybeer/toolrecall/issues/new"
        })
        self.assertIsNone(err)
        _log("  PASS: URL allowed")

    # ─── Edge cases ─────────────────────────────────

    def test_ast_skips_non_string_values(self):
        """AST-014: Non-string values (int, list, dict) are skipped."""
        _log("Testing non-string value skip")
        security = SecurityGate(self.mock_cfg)
        err = security.check_ast_injection({
            "count": 42,
            "items": [1, 2, 3],
            "config": {"key": "value"},
            "enabled": True,
        })
        self.assertIsNone(err)
        _log("  PASS: Non-string values skipped")

    def test_ast_skips_short_strings(self):
        """AST-015: Strings under 10 chars are skipped."""
        _log("Testing short string skip")
        security = SecurityGate(self.mock_cfg)
        err = security.check_ast_injection({
            "name": "hi",
            "flag": "x",
        })
        self.assertIsNone(err)
        _log("  PASS: Short strings skipped")

    def test_ast_disabled_when_ast_check_off(self):
        """AST-016: AST check is skipped when config disables it."""
        _log("Testing disabled AST check")
        self.mock_cfg.mcp_ast_check_enabled = False
        security = SecurityGate(self.mock_cfg)
        err = security.check_ast_injection({"code": "exec('rm')"})
        self.assertIsNone(err, "AST check must be skipped when disabled")
        _log("  PASS: AST check disabled correctly")

    # ─── Performance ────────────────────────────────

    def test_ast_performance(self):
        """AST-017: AST check completes in <0.1ms per call."""
        _log("Testing AST check performance")
        security = SecurityGate(self.mock_cfg)
        args = {
            "content": '{"items": [1, 2, 3], "name": "test"}',
            "path": "/home/user/file.txt",
            "description": "A long natural language description that should pass through",
            "url": "https://example.com/api/v1/resources",
        }
        # Warmup
        _ = security.check_ast_injection(args)
        # Measure
        repetitions = 1000
        start = time.perf_counter()
        for _ in range(repetitions):
            security.check_ast_injection(args)
        elapsed = time.perf_counter() - start
        per_call_ms = (elapsed / repetitions) * 1000
        _log(f"  Average: {per_call_ms:.4f}ms per call ({repetitions} reps)")
        self.assertLess(per_call_ms, 0.1,
                        f"AST check too slow: {per_call_ms:.4f}ms (threshold: 0.1ms)")
        _log("  PASS: Performance within 0.1ms threshold")

    # ─── Integration: code that happens to look like Python ───

    def test_ast_allows_short_math(self):
        """AST-018: Short mathematical expressions pass through (single statement, not code)."""
        _log("Testing math expression")
        security = SecurityGate(self.mock_cfg)
        # "1+1" is under 10 chars, so it's skipped by length check
        err = security.check_ast_injection({"expr": "1 + 2 * 3"})
        self.assertIsNone(err)
        _log("  PASS: Short math expression allowed")

    def test_ast_blocks_multi_line_code(self):
        """AST-019: Multi-line code block with exec is blocked."""
        _log("Testing multi-line code block")
        security = SecurityGate(self.mock_cfg)
        code = """
import os
result = os.popen('cat /etc/shadow').read()
print(result)
"""
        err = security.check_ast_injection({"script": code})
        self.assertIsNotNone(err)
        self.assertIn("import statement", err)
        _log("  PASS: Multi-line code with import blocked")


if __name__ == "__main__":
    unittest.main()
