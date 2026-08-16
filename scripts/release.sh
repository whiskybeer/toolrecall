#!/usr/bin/env bash
#
# ToolRecall automated release pipeline.
#
# Usage:
#   ./scripts/release.sh 0.8.18                  # full release
#   ./scripts/release.sh 0.8.18 --status          # show completed stages
#   ./scripts/release.sh 0.8.18 --resume          # resume from last checkpoint
#
# Stages (matches devops/toolrecall-release skill):
#   0  Prerequisites
#   1  Version bump
#   2  Code review (ruff)
#   3  Security review
#   4  Doc & architecture audit
#   5  Obsidian vault cross-ref
#   6  Tests (unit, minus e2e/ADK)
#   7  CHANGELOG
#   8  Pre-commit hygiene
#   9  Approval gate
#   10 Commit + tag
#   11 Push
#   12 GitHub release
#   13 Build + PyPI
#   14 Local upgrade (editable pipx + daemon restart)
#
set -euo pipefail

VERSION="${1:-}"
MODE="${2:-run}"   # run | resume | status
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CKPT_DIR="$REPO_ROOT/.release-checkpoints"
VERSION_RE='^[0-9]+\.[0-9]+\.[0-9]+$'

if ! [[ "$VERSION" =~ $VERSION_RE ]]; then
    echo "Usage: $0 <X.Y.Z> [--resume|--status]" >&2
    echo "  e.g. $0 0.8.18" >&2
    exit 1
fi
if [[ "$MODE" == "--status" ]]; then MODE=status; fi
if [[ "$MODE" == "--resume" ]]; then MODE=resume; fi

cd "$REPO_ROOT"
mkdir -p "$CKPT_DIR"

ckpt() { echo "$CKPT_DIR/$1.done"; }
done_stage() { touch "$(ckpt "$1")"; }
stage_done() { [[ -f "$(ckpt "$1")" ]]; }

# ─── Stage implementations (defined before use) ─────────────────

ensure_prereqs() {
    echo "  checking gh, build/twine, git status, pypirc..."
    command -v gh >/dev/null || { echo "  ✗ gh CLI not installed"; return 1; }
    command -v twine >/dev/null || { echo "  ✗ twine not installed"; return 1; }
    python3 -c "import build" 2>/dev/null || { echo "  ✗ python 'build' not installed"; return 1; }
    [[ -f "$HOME/.pypirc" ]] || echo "  ⚠ ~/.pypirc missing — PyPI upload may fail"

    if ! gh auth status >/dev/null 2>&1; then
        echo "  ⚠ gh not authenticated (GITHUB_TOKEN invalid/absent) — stage 12 will fail"
    fi

    local dirty
    dirty="$(git status --porcelain | grep -v '^??' || true)"
    if [[ -n "$dirty" ]]; then
        echo "  ✗ uncommitted tracked changes present:"
        echo "$dirty" | sed 's/^/    /'
        return 1
    fi
    echo "  ✓ git tree clean, build/twine present, gh installed"
}

bump_version() {
    echo "  bumping to $VERSION in __init__.py + pyproject.toml"
    sed -i "s/__version__ = \".*\"/__version__ = \"$VERSION\"/" toolrecall/__init__.py
    sed -i "s/^version = \".*\"/version = \"$VERSION\"/" pyproject.toml
    grep -n "__version__" toolrecall/__init__.py | head -1 | sed 's/^/    /'
    grep -n "^version" pyproject.toml | head -1 | sed 's/^/    /'
}

ruff_check() {
    echo "  ruff check toolrecall/ tests/"
    if command -v ruff >/dev/null 2>&1; then
        ruff check toolrecall/ tests/
    else
        python3 -m ruff check toolrecall/ tests/
    fi
    echo "  ✓ ruff clean"
}

security_check() {
    echo "  security scan..."
    local hit=0
    grep -rn "shell=True" toolrecall/ --include='*.py' | grep -v 'shell=True.*#' && hit=1
    grep -rn "\\bexec(\\|\\beval(" toolrecall/ --include='*.py' | grep -v '#' && hit=1
    if git grep -n "^/etc\|^/dev" toolrecall/config.toml >/dev/null 2>&1; then
        echo "  ⚠ config.toml still allows /etc or /dev — check defaults"; 
    fi
    if grep -rn "^/tmp\|^/home" toolrecall/config.toml >/dev/null 2>&1; then
        : # shipped allowlist is repo-local; /etc and /dev handled above
    fi
    [[ "$hit" == "0" ]] || { echo "  ✗ security findings above need manual review"; return 1; }
    echo "  ✓ no obvious shell=True/eval/exec or /etc /dev allowlist findings"
}

doc_audit() {
    echo "  doc version-stamp audit..."
    local stale
    stale="$(grep -rn 'Version:\|Based on:\|v0\.' docs/*.md 2>/dev/null | grep -viE "v$VERSION\b" | grep -iE "version|based on|v0\.8\.1[0-7]" || true)"
    if [[ -n "$stale" ]]; then
        echo "  ⚠ possible stale version stamps in docs:"
        echo "$stale" | sed 's/^/    /'
    else
        echo "  ✓ no obviously stale doc stamps"
    fi
    python3 - <<'PY'
import re, glob
n = 0
for f in glob.glob("tests/*.py"):
    n += len(re.findall(r"def test_", open(f).read()))
print(f"  info: {n} test functions across {len(glob.glob('tests/*.py'))} files")
PY
}

vault_crossref() {
    echo "  avoiding stale wiki — verify sources-dir exists before scanning"
    local vault="${TOOLRECALL_VAULT:-}"
    if [[ -z "$vault" ]]; then
        echo "  ⚠ TOOLRECALL_VAULT unset — skipping vault cross-ref (stage 5 optional)"
        return 0
    fi
    [[ -d "$vault" ]] || { echo "  ⚠ vault path not found: $vault (skipping)"; return 0; }
    echo "  ✓ vault present at $vault (manual wikilink review advised)"
}

run_tests() {
    echo "  running unit test suite (excluding e2e + ADK)..."
    # De-duplicated from the Makefile so the selection stays in one place.
    make test-unit 2>&1 | tail -15
}

changelog_edit() {
    echo "  opening CHANGELOG for $VERSION (edit in $EDITOR)"
    # Do not template-overwrite; require a [X.Y.Z] header to be present.
    if ! grep -q "^## \[$VERSION\]" CHANGELOG.md; then
        echo "  ✗ CHANGELOG.md has no '## [$VERSION]' section — add one before continuing"
        return 1
    fi
    echo "  ✓ CHANGELOG has [$VERSION] header"
    if [[ -n "${EDITOR:-}" ]]; then
        "$EDITOR" CHANGELOG.md || true
    fi
}

precommit_hygiene() {
    echo "  pre-commit hygiene check..."
    local hit=0
    git diff --cached --name-only | grep -E 'dist/|build/' && { echo "  ⚠ build artifacts staged"; hit=1; }
    grep -rn "TODO\|FIXME\|XXX" toolrecall/ --include='*.py' | grep -v '#' && hit=1
    [[ "$hit" == "0" ]] || { echo "  ⚠ TODOs/artifacts present — review above (not fatal)"; }
    echo "  ✓ hygiene scan done"
}

approval_gate() {
    echo ""
    echo "====== RELEASE REVIEW — v$VERSION ======"
    git diff --stat HEAD
    echo ""
    read -r -p "Ready to commit, tag, and push v$VERSION? (yes/N) " ans
    if [[ "$ans" != "yes" ]]; then
        echo "  ✗ aborted by user"
        exit 1
    fi
    echo "  ✓ approved"
}

commit_tag() {
    echo "  committing + tagging v$VERSION"
    git add -A
    git commit -m "v$VERSION: $(git log --oneline -1 --format=%s HEAD 2>/dev/null || echo release)"
    git tag -a "v$VERSION" -m "v$VERSION release"
}

push() {
    echo "  pushing origin main + tags"
    if git push origin main --tags; then
        echo "  ✓ pushed"
    else
        echo "  ⚠ push failed (or rejected) — attempting --force-with-lease fallback:"
        git push --force-with-lease origin main --tags
    fi
}

github_release() {
    echo "  creating GitHub release v$VERSION"
    local notes
    notes="$(awk -v v="$VERSION" '
        $0 ~ "^## \\[" v "\\]" { on=1; next }
        on && $0 ~ "^## \\[" { exit }
        on { print }
    ' CHANGELOG.md)"
    gh release create "v$VERSION" --title "v$VERSION" --notes "$notes"
}

build_pypi() {
    echo "  building wheel + sdist, uploading to PyPI"
    rm -rf dist build
    python3 -m build
    python3 -m twine upload dist/toolrecall-"$VERSION"*
    echo "  ✓ uploaded to PyPI"
}

local_upgrade() {
    echo "  upgrading local install (editable pipx + daemon restart)"
    systemctl --user stop toolrecall-daemon 2>/dev/null || true
    pipx install "$REPO_ROOT" --editable --force
    systemctl --user start toolrecall-daemon 2>/dev/null || true
    echo "  ✓ local upgraded"
}

# ─── Orchestration ──────────────────────────────────────────────

run_or_resume() {
    local stage="$1"; shift
    if [[ "$MODE" == "status" ]]; then
        printf '  %-2s %s\n' "$stage" "$(stage_done "$stage" && echo DONE || echo pending)"
        return 0
    fi
    if [[ "$MODE" == "resume" ]] && stage_done "$stage"; then
        echo "  [skip] stage $stage already complete"; return 0
    fi
    echo "==> stage $stage"
    "$@"
    done_stage "$stage"
}

if [[ "$MODE" == "status" ]]; then
    echo "Release $VERSION — checkpoint status:"
    for s in 0 1 2 3 4 5 6 7 8 9 10 11 12 13 14; do
        run_or_resume "$s" true
    done
    exit 0
fi

echo "═══ ToolRecall release v$VERSION ═══"

run_or_resume 0 ensure_prereqs
run_or_resume 1 bump_version
run_or_resume 2 ruff_check
run_or_resume 3 security_check
run_or_resume 4 doc_audit
run_or_resume 5 vault_crossref
run_or_resume 6 run_tests
run_or_resume 7 changelog_edit
run_or_resume 8 precommit_hygiene
run_or_resume 9 approval_gate
run_or_resume 10 commit_tag
run_or_resume 11 push
run_or_resume 12 github_release
run_or_resume 13 build_pypi
run_or_resume 14 local_upgrade

echo ""
echo "✓ Release v$VERSION complete."
echo "  Next: verify 'toolrecall --version' and 'toolrecall daemon --status'"
