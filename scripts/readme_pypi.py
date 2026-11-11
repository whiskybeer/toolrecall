#!/usr/bin/env python3
"""Generate README-pypi.md from README.md for PyPI.

PyPI's description renderer leaves relative links untouched, so every
relative link in README.md 404s on pypi.org (it resolves against
pypi.org/project/toolrecall/...). This script rewrites all relative links
to absolute GitHub URLs so the PyPI page works.

Usage:
    python3 scripts/readme_pypi.py            # generate README-pypi.md
    python3 scripts/readme_pypi.py --check    # verify README-pypi.md is current (release gate)
"""
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
README = REPO_ROOT / "README.md"
OUT = REPO_ROOT / "README-pypi.md"
GITHUB_BLOB = "https://github.com/whiskybeer/toolrecall/blob/main/"

# Markdown link pattern: [text](target). Deliberately skips bare URLs and
# autolinks, which are already absolute or intentionally local (#anchors).
LINK_RE = re.compile(r"(\[[^\]]*\])\(([^)\s]+)\)")


def rewrite(text: str) -> tuple[str, int]:
    """Rewrite relative link targets to absolute GitHub blob URLs."""
    count = 0

    def repl(m: re.Match) -> str:
        nonlocal count
        text_part, target = m.group(1), m.group(2)
        if target.startswith(("http://", "https://", "mailto:", "#")):
            return m.group(0)
        path = target.lstrip("./")
        count += 1
        return f"{text_part}({GITHUB_BLOB}{path})"

    new_text = LINK_RE.sub(repl, text)
    return new_text, count


def main() -> int:
    check = "--check" in sys.argv
    text = README.read_text(encoding="utf-8")
    out, n = rewrite(text)
    if check:
        if not OUT.exists():
            print("FAIL: README-pypi.md missing; run scripts/readme_pypi.py", file=sys.stderr)
            return 1
        current = OUT.read_text(encoding="utf-8")
        if current != out:
            print("FAIL: README-pypi.md stale; regenerate it", file=sys.stderr)
            return 1
        print(f"OK: README-pypi.md current ({n} relative links rewritten)")
        return 0
    OUT.write_text(out, encoding="utf-8", newline="\n")
    print(f"OK: wrote {OUT} ({n} relative links rewritten)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
