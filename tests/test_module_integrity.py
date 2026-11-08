"""Package & docs integrity guards: docstrings + docs-to-code sync.

Two invariants that previously drifted:

1. Every ``toolrecall/**/*.py`` module carries a module docstring
   (package self-description; docstring is the module map source).
2. Every ``toolrecall/<path>.py`` file cited in ``docs/`` or
   ``README.md`` actually exists — catches stale references after
   modules are renamed or retired (e.g. ``toolrecall/security.py``,
   whose SecurityGate now lives in ``toolrecall/daemon.py``).
"""

import ast
import pathlib
import re
import unittest

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_PKG = _ROOT / "toolrecall"
_DOCS = _ROOT / "docs"
_DOC_FILES = sorted(_DOCS.glob("*.md")) + [_ROOT / "README.md"]
# Captures the full path after the package prefix, incl. subpackage dirs
# (e.g. "daemon.py", "adapters/litellm.py", "storage/sqlite.py").
_PY_REF = re.compile(r"\btoolrecall/([a-z_][a-z_0-9]*(?:/[a-z_][a-z_0-9]*)*\.py)\b")


def _all_package_modules() -> list[pathlib.Path]:
    return sorted(_PKG.rglob("*.py"))


class TestModuleDocstrings(unittest.TestCase):
    """Every shipped module carries a module docstring."""

    def test_all_modules_have_docstrings(self) -> None:
        missing = [
            p.relative_to(_ROOT).as_posix()
            for p in _all_package_modules()
            if not ast.get_docstring(ast.parse(p.read_text(encoding="utf-8"), filename=str(p)))
        ]
        self.assertEqual(missing, [], f"modules without docstrings: {missing}")


class TestDocsReferenceRealModules(unittest.TestCase):
    """``toolrecall/*.py`` paths cited in docs must exist (no stale refs)."""

    def test_documented_module_paths_exist(self) -> None:
        stale: list[str] = []
        url = re.compile(r"https?://\S+")  # GitHub/web links cite remote paths, not local files
        for md in _DOC_FILES:
            text = url.sub(" ", md.read_text(encoding="utf-8", errors="replace"))
            for m in _PY_REF.finditer(text):
                rel = m.group(1)
                if not (_PKG / rel).exists():
                    stale.append(f"{md.relative_to(_ROOT).as_posix()}: toolrecall/{rel}")
        self.assertEqual(stale, [], f"stale module references: {stale}")


if __name__ == "__main__":
    unittest.main()
