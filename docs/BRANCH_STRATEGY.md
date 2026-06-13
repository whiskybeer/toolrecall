# ToolRecall Branch Strategy

## Branches

| Branch | Purpose | Base |
|--------|---------|------|
| `main` | **Release-ready.** Bugfixes only. Every commit on `main` is a potential release. | — |
| `feature/` | New features, refactors, experiments. Prefix: `feature/<name>`. | `main` |
| `test` | Test-only branch. All test improvements, new test suites, CI changes. | `main` |
| `fix/` | Hotfixes for `main` when `main` is too dirty for a direct commit. Prefix: `fix/<name>`. | `main` |

## Rules

### 1. `main` is sacred
- Direct commits only for **bugfixes** and **critical patches**.
- No features on `main`. No experiments. No half-finished work.
- Every commit on `main` should be release-candidate quality.

### 2. New features → `feature/<name>`
- Branch from `main`.
- Work there. Commit freely. Break things.
- When done and tested: create a PR (or merge) into `main`.
- Name: `feature/<short-description>` (e.g., `feature/redis-backend`).

### 3. Test improvements → `test`
- All test changes: new tests, test infrastructure, CI config, benchmarks.
- Can merge into `main` independently of features.
- Keeps test history clean and traceable.

### 4. Hotfixes → `fix/<name>`
- Only when `main` has uncommitted or unreleased changes that block a fix.
- Branch from `main`, fix, merge back.

## Workflow

```text
main         ───●────────────●─────────────●──→
                 │            │             │
feature/redis    └──●──●──●───┘             │
                                            │
test             └──●──●────────────────────┘
                 (tests pass independently)
```

## Release Process

1. All changes for the release are on `main`.
2. Bump version (`__init__.py` + `pyproject.toml`).
3. Tag: `git tag v<major>.<minor>.<patch>`
4. Push tag: `git push origin v<major>.<minor>.<patch>`
5. Publish to PyPI.
