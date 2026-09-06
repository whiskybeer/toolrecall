"""Silent-swallow audit — a meta-test that freezes the except-pass inventory.

Rationale: the two worst production bugs in ToolRecall's history (v0.8.13
forward proxy not listening; benchmark arms silently running identically)
were both caused by swallowed exceptions. This test makes the swallow
inventory VISIBLE and MONOTONICALLY IMPROVING:

* Truly bare ``except:`` (no exception type) — count must be ZERO. There is
  no legitimate use for these; they catch KeyboardInterrupt and SystemExit.
* Typed ``except <Type>: pass`` — the per-file count is FROZEN. A test
  failure means someone ADDED a swallow handler. Either narrow the exception
  type, log the error, or propagate a status flag (see the skill
  ``silent-error-patterns`` for the data-quality variant), then update the
  FROZEN_COUNTS baseline in this file in the same commit — deliberately
  requiring a human-adjacent review step, never a silent increase.

To REDUCE a frozen count: fix the handler, then lower the number here.
"""

import ast
import pathlib
import unittest

PACKAGE_DIR = pathlib.Path(__file__).resolve().parent.parent / "toolrecall"

# Frozen per-file counts of *typed* except handlers whose body is only `pass`.
# Baseline measured 2026-09-01 across toolrecall/*.py (62 total).
# Rule: decreases are always welcome (just edit the number). Increases fail CI.
FROZEN_COUNTS = {
    "_db.py": 8,
    "cli.py": 7,
    "client.py": 1,
    "config.py": 3,
    "daemon.py": 18,
    "docs.py": 4,
    "healthcheck.py": 2,
    "mcp_bridge.py": 4,
    "mcp_cache_fs.py": 3,
    "normalizer.py": 1,
    "proxy.py": 2,
    "shim.py": 4,
    "transport.py": 0,
    "updater.py": 2,  # auto-updater: network-fetch fail-soft + optional metadata probe (see module docstrings)
    "venvs.py": 3,
}

# Narrow exception types that are ALWAYS acceptable to swallow silently —
# they express "optional thing absent" and have a known recovery path.
# (From the skill's valid-case table.) These do NOT need review to add.
SAFE_TYPES = {
    "FileNotFoundError",
    "KeyError",
    "KeyboardInterrupt",
    "StopIteration",
}


def _except_type_name(node: ast.ExceptHandler) -> str:
    if node.type is None:
        return "<bare>"
    return ast.unparse(node.type)


def _collect_swallows():
    """Return (bare_bare_count, per_file_typed_counts dict, findings list)."""
    bare = 0
    per_file = {}
    findings = []
    for f in sorted(PACKAGE_DIR.glob("*.py")):
        tree = ast.parse(f.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ExceptHandler):
                continue
            if not all(isinstance(s, ast.Pass) for s in node.body):
                continue
            tname = _except_type_name(node)
            loc = f"{f.relative_to(PACKAGE_DIR.parent)}:{node.lineno}: except {tname}"
            if node.type is None:
                bare += 1
                findings.append(("BARE", loc))
            else:
                # Strip tuple/nesting to check "is every member safe?"
                parts = {p.strip() for p in tname.replace("(", "").replace(")", "").split(",")}
                if parts <= SAFE_TYPES:
                    continue  # narrow optional-lookup swallow — always fine
                per_file[str(f.name)] = per_file.get(str(f.name), 0) + 1
    return bare, per_file, findings


class TestSilentSwallowInventory(unittest.TestCase):
    def test_no_bare_except_pass(self):
        """Bare `except:` + pass is never legitimate. Must stay at zero."""
        bare, _, findings = _collect_swallows()
        self.assertEqual(
            bare,
            0,
            "Bare except: pass found — catches KeyboardInterrupt/SystemExit. "
            "Give it a concrete exception type:\n  " + "\n  ".join(loc for _, loc in findings),
        )

    def test_typed_swallow_count_not_growing(self):
        """New typed except-pass handlers require an explicit baseline bump."""
        current, findings = {}, []
        for f in sorted(PACKAGE_DIR.glob("*.py")):
            tree = ast.parse(f.read_text(encoding="utf-8"))
            n = 0
            for node in ast.walk(tree):
                if not isinstance(node, ast.ExceptHandler):
                    continue
                if not all(isinstance(s, ast.Pass) for s in node.body):
                    continue
                if node.type is None:
                    continue  # covered by the bare test
                tname = _except_type_name(node)
                parts = {p.strip() for p in tname.replace("(", "").replace(")", "").split(",")}
                if parts <= SAFE_TYPES:
                    continue
                n += 1
                findings.append(f"{f.relative_to(PACKAGE_DIR.parent)}:{node.lineno}")
                current[f.name] = current.get(f.name, 0) + 1

        grew = {
            f: (cur, FROZEN_COUNTS.get(f, 0))
            for f, cur in current.items()
            if cur > FROZEN_COUNTS.get(f, 0)
        }
        new_files = set(current) - set(FROZEN_COUNTS)
        self.assertFalse(
            grew or new_files,
            "New silent swallow handler(s) detected:\n  "
            + "\n  ".join(f"{f}: {c} > baseline {b}" for f, (c, b) in grew.items())
            + ("\n  new files: " + ", ".join(sorted(new_files)) if new_files else "")
            + "\n\nLocations:\n  "
            + "\n  ".join(findings)
            + "\n\nFix: narrow the exception type, log the error, or propagate "
            "a status flag. If the swallow is genuinely correct, update "
            "FROZEN_COUNTS in this file in the same commit with a comment "
            "justifying it.",
        )

    def test_inventory_matches_baseline_honestly(self):
        """Baseline drifts downward over time; detect stale entries + typos.

        Fails only if the CURRENT total exceeds the sum of the frozen
        baseline (growth is caught above); stale zero-entries are reported
        as a warning-style assertion so the table stays honest.
        """
        current_total = sum(_collect_swallows()[1].values())
        frozen_total = sum(FROZEN_COUNTS.values())
        self.assertLessEqual(
            current_total,
            frozen_total,
            f"Swallow inventory grew: {current_total} > frozen {frozen_total}. "
            "See test_typed_swallow_count_not_growing for locations.",
        )
        # Files listed in the baseline but gone from the package = stale.
        package_files = {f.name for f in PACKAGE_DIR.glob("*.py")}
        stale = set(FROZEN_COUNTS) - package_files
        self.assertEqual(
            stale,
            set(),
            f"FROZEN_COUNTS references files that no longer exist: {sorted(stale)}. "
            "Remove the stale entries.",
        )


if __name__ == "__main__":
    unittest.main()
