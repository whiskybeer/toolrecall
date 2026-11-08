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
    # 1 as of 2026-09-17 (baseline sync): the lazy-init rewrite (ba1a594 era)
    # replaced seven swallows with real handling; the remaining one guards
    # the config re-read around the backend switch (fallback path below it).
    "_db.py": 1,
    # 8 as of 2026-09-11: pre-existing drift found when make validate re-ran
    # after e89a5b6 — counted Exception-pass sites (daemon ping probes,
    # shim-optional fallback) predate the baseline; WIP added none.
    "cache.py": 0,
    "cli.py": 8,
    "client.py": 1,
    # 2 as of 2026-09-17 (baseline sync): 1d33b12 (update hint channel)
    # returned errors instead of swallowing on one path.
    "config.py": 3,
    # 3 as of 2026-09-21 (TMPDIR-nesting fixes): +1 in tracker_ignore_read_prefixes —
    # the tempdir carve-out (drop default prefixes that contain gettempdir(), so
    # Hermes-scratch test reads are not silently untracked) is fail-soft: a broken
    # tempdir leaves the prefix list unfiltered, exactly the pre-fix classification.
    # 2 as of 2026-09-08 (was 18): +4 best-effort socket/PID closes added by
    # the 956b25a network-honesty commit without a baseline bump (found when
    # the flock-sweep work re-ran validate). All four are resource-release
    # fail-soft (conn/server close, PID-file write) — socket ping and the
    # kernel flock are authoritative, so the swallowed errors carry no signal.
    # 23 as of 51aff04 (A6 review): +1 srv.close() OSError pass on the
    # TLS-refusal shutdown path — listener cleanup before exit, no signal.
    # 24 as of 2026-09-15 (auto_apply=false hint channel): +1 in
    # _run_update_refresher — hourly best-effort PyPI re-check on the daemon
    # background thread; failures just leave latest_seen stale (the CLI hook
    # and the update_pending_hint handler both tolerate that), and a network
    # hiccup must never take the daemon down.
    # 26 as of daa820b (MCP server reap, 2026-10-03): +2 in
    # shutdown_all_mcp_sessions / MCPClientSession teardown — best-effort
    # process reaps at exit/GC; a dead child must not break the shutdown
    # path it exists to clean up.
    "daemon.py": 26,
    # 2 as of 2026-09-17 (baseline sync): 3573f4d (agent-agnostic scan_dirs)
    # refactored two swallows away.
    "docs.py": 2,
    "healthcheck.py": 2,
    # 5 as of 2026-09-17 (baseline sync): 6 pre-sync minus one converted to a
    # debug log by the stub_core split (dedup_tokens counter); the three
    # hint/update round-trip swallows are now logged, leaving the two
    # config-hash-store OSError guards.
    # 4 as of 2026-09-24 (baseline sync): +2 config-hash continuity store
    # handlers (read + write around the "config changed since last
    # connection" warning, cache-instrument merges). Pure advisory
    # bookkeeping: a failed read/write only skips one warning header.
    "mcp_bridge.py": 4,
    # 2 as of 2026-09-24 (baseline sync): numeric parse guards in the TUI
    # stats formatting path (TypeError/ValueError -> pass) — malformed
    # stat values fall back to the row rendering without them; the
    # continue/assignment siblings are not counted (non-pass bodies).
    "trend.py": 2,
    "mcp_cache_fs.py": 3,
    "normalizer.py": 1,
    # 4 as of 2026-09-10 (stream usage truth): +1 in _parse_sse_usage_tail —
    # best-effort parse of the final SSE chunk's usage block. Any malformed
    # tail must degrade to {} so the caller falls back to the byte estimate;
    # the swallow carries no signal (usage absence is a normal provider case).
    # 5 as of 2026-09-17 (baseline sync): +1 in the extra_hosts loader
    # (3fc2ca0) — config unavailable (tests/embedding) must degrade to the
    # hardcoded list; the fallback is the documented behavior.
    "proxy.py": 5,
    "shim.py": 4,
    # 4 as of 2026-09-21 (TMPDIR-nesting fixes): +1 in _looks_like_library_path —
    # markers are matched against the path relative to gettempdir() (tempdir's
    # own path must not satisfy /.hermes/ etc.); OSError → unstripped norm,
    # the pre-fix classification. Guard sits on the open() hot path.
    # 3 as of 2026-09-17 (stub_core split): was 4 — the big cache-path
    # fall-through now logs a warning instead of passing silently.
    # 0 as of 2026-09-17: env-TTL parse + config resolution failures are
    # logged at debug (shim never owns a log file; lastResort carries them).
    "shim_stub.py": 0,
    # 7 as of 2026-09-19 (cache-instrument): +2 — cache_read/cache_discount
    # CSV column sums in money_today() (typed (TypeError, ValueError), same
    # shape as the existing dedup sum; column-absent rows → 0 back-compat).
    "tui_top.py": 7,
    "transport.py": 0,
    # 1 as of 2026-09-17 (baseline sync): 71ebf9f SAST pass fixed one.
    "updater.py": 1,
    # 3 as of 2026-09-17 (baseline sync): c2d516d venv upgrade pass dropped one.
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
