# Copyright (c) 2026 Robin Schultka
# SPDX-License-Identifier: MIT
# Source: https://github.com/whiskybeer/toolrecall
"""Tests for auto-indexing Antigravity skills, plugins, and artifacts."""

from toolrecall import docs


def test_index_antigravity_custom_roots(tmp_path, monkeypatch):
    db_file = tmp_path / "test_knowledge.db"
    monkeypatch.setenv("TOOLRECALL_KNOWLEDGE_DB", str(db_file))

    # Create mock skills
    skill_dir = tmp_path / "skills" / "my-skill"
    skill_dir.mkdir(parents=True)
    skill_md = skill_dir / "SKILL.md"
    skill_md.write_text(
        "# My Skill\n\nThis is an automated workflow for deploying microservices with Antigravity.\n",
        encoding="utf-8",
    )

    counts = docs.index_antigravity(include_artifacts=False, custom_roots=[str(skill_dir)])
    assert counts.get("total", 0) >= 1
    assert counts.get("antigravity-skills", 0) >= 1

    # Verify searchable via docs_search
    res = docs.docs_search("microservices Antigravity")
    assert "My Skill" in res
    assert "antigravity-skills" in res


def test_index_antigravity_artifacts_filter(tmp_path, monkeypatch):
    db_file = tmp_path / "test_knowledge2.db"
    monkeypatch.setenv("TOOLRECALL_KNOWLEDGE_DB", str(db_file))

    brain_dir = tmp_path / "brain" / "conv-123"
    brain_dir.mkdir(parents=True)
    report_md = brain_dir / "report.md"
    report_md.write_text(
        "# Benchmark Report\n\nPerformance was 10x faster with ToolRecall.\n", encoding="utf-8"
    )

    # System generated should be ignored
    sys_dir = brain_dir / ".system_generated"
    sys_dir.mkdir()
    (sys_dir / "ignored.md").write_text("# Ignored\n", encoding="utf-8")

    # Mock candidate search paths to tmp_path
    monkeypatch.setattr(
        "os.path.expanduser",
        lambda p: str(tmp_path / "brain") if "brain" in p else p,
    )

    counts = docs.index_antigravity(include_artifacts=True)
    assert counts.get("antigravity-artifacts", 0) >= 1

    res = docs.docs_search("Benchmark Report", source="antigravity-artifacts")
    assert "Benchmark Report" in res
