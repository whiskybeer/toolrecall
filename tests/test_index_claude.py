# Copyright (c) 2026 Robin Schultka
# SPDX-License-Identifier: MIT
# Source: https://github.com/whiskybeer/toolrecall
"""Tests for auto-indexing Claude Code commands, skills, and agent-agnostic indexing."""

from toolrecall import docs


def test_index_claude_code_custom_roots(tmp_path, monkeypatch):
    db_file = tmp_path / "test_knowledge_cc.db"
    monkeypatch.setenv("TOOLRECALL_KNOWLEDGE_DB", str(db_file))

    # Create mock claude commands / skills
    cmd_dir = tmp_path / "commands"
    cmd_dir.mkdir(parents=True)
    cmd_md = cmd_dir / "review.md"
    cmd_md.write_text(
        "# Code Review Command\n\nRun git diff review with Claude Code.\n",
        encoding="utf-8",
    )

    counts = docs.index_claude_code(custom_roots=[str(cmd_dir)])
    assert counts.get("total", 0) >= 1
    assert counts.get("claude-commands", 0) >= 1

    # Verify searchable via docs_search
    res = docs.docs_search("review Claude Code")
    assert "Code Review Command" in res
    assert "claude-commands" in res


def test_index_all_agents_runs_safely(tmp_path, monkeypatch):
    db_file = tmp_path / "test_knowledge_all.db"
    monkeypatch.setenv("TOOLRECALL_KNOWLEDGE_DB", str(db_file))

    counts = docs.index_all_agents()
    assert isinstance(counts, dict)
    assert "total" in counts
