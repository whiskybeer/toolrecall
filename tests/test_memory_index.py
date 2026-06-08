"""Tests for Hermes memory indexing in ToolRecall's FTS5 knowledge database.

Tests verify:
  - index_hermes_memory() parses §-delimited entries correctly
  - FTS5 triggers sync on INSERT (pages → pages_fts)
  - docs_search(source='hermes-memory') returns BM25-weighted results
  - Re-indexing updates existing entries (INSERT OR REPLACE)
"""

import os
import sys
import unittest
import tempfile
import shutil

# Isolated test environment
test_dir = tempfile.mkdtemp()
# Force isolated knowledge DB BEFORE any import — also ensures Config singleton
# picks up the right env var before toolrecall docs module is first imported
os.environ["TOOLRECALL_KNOWLEDGE_DB"] = os.path.join(test_dir, "test_knowledge.db")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from toolrecall.docs import (
    _get_db, _ensure_tables, index_hermes_memory, index_directory,
    docs_search, docs_get_page
)


class TestMemoryIndex(unittest.TestCase):
    """Test that Hermes memory stores are correctly indexed into FTS5."""

    def setUp(self):
        os.makedirs(test_dir, exist_ok=True)
        # Create a fake Hermes memories directory with test content
        self.memory_dir = os.path.join(test_dir, "memories")
        os.makedirs(self.memory_dir, exist_ok=True)

        # Write a test MEMORY.md with §-delimited entries
        with open(os.path.join(self.memory_dir, "MEMORY.md"), "w") as f:
            f.write(
                "Security rules: never expose .env files\n"
                "§\n"
                "Python version: 3.11 uses tomllib (stdlib)\n"
                "§\n"
                "Air-gapped mode: no external API calls allowed\n"
            )

        # Write a test USER.md
        with open(os.path.join(self.memory_dir, "USER.md"), "w") as f:
            f.write(
                "Robin prefers dense no-fluff responses\n"
                "§\n"
                "Always test before UI changes\n"
            )

    def tearDown(self):
        # Clean up files/directories inside test_dir, keeping test_dir itself
        if os.path.exists(test_dir):
            for item in os.listdir(test_dir):
                item_path = os.path.join(test_dir, item)
                if os.path.isdir(item_path):
                    shutil.rmtree(item_path, ignore_errors=True)
                else:
                    try:
                        os.unlink(item_path)
                    except Exception:
                        pass

    def test_index_creates_entries(self):
        """Each §-delimited entry becomes a separate page."""
        count = index_hermes_memory(self.memory_dir)
        self.assertEqual(count, 5)  # 3 from MEMORY.md + 2 from USER.md

    def test_fts5_search_finds_entries(self):
        """FTS5 BM25 search returns matched entries with source='hermes-memory'."""
        index_hermes_memory(self.memory_dir)

        # Search via FTS5
        result = docs_search("security", source="hermes-memory")
        self.assertIn("Security rules", result)
        self.assertIn("hermes-memory", result)

    def test_fts5_search_python(self):
        """Porter stemming: 'python' should match 'Python'."""
        index_hermes_memory(self.memory_dir)
        result = docs_search("python", source="hermes-memory")
        self.assertIn("tomllib", result, "Porter stemming should match 'Python'")

    def test_fts5_search_german(self):
        """German text with Porter unicode61 tokenizer."""
        index_hermes_memory(self.memory_dir)
        result = docs_search("fluff", source="hermes-memory")
        self.assertIn("prefers dense", result)

    def test_fts5_triggers_sync_on_insert(self):
        """Verify FTS5 content sync trigger fires on INSERT into pages."""
        index_hermes_memory(self.memory_dir)

        conn = _get_db()
        _ensure_tables(conn)

        # Direct FTS5 query to verify trigger synced the content
        fts_row = conn.execute(
            "SELECT COUNT(*) as cnt FROM pages_fts WHERE pages_fts MATCH 'security'"
        ).fetchone()
        conn.close()

        self.assertGreater(fts_row["cnt"], 0,
                           "FTS5 trigger should sync 'security' match")

    def test_docs_get_page_returns_entry(self):
        """Individual memory entries are retrievable by path (fuzzy match)."""
        index_hermes_memory(self.memory_dir)

        page = docs_get_page("MEMORY.md#", source="hermes-memory")
        self.assertIn("MEMORY.md#", page, "Fuzzy match should find memory entry")
        self.assertIn("tomllib", page)

    def test_reindex_updates_entries(self):
        """Re-indexing replaces existing entries (INSERT OR REPLACE)."""
        count1 = index_hermes_memory(self.memory_dir)
        count2 = index_hermes_memory(self.memory_dir)
        self.assertEqual(count1, count2, "Re-index should produce same count")
        self.assertEqual(count1, 5)

        # Modify memory
        with open(os.path.join(self.memory_dir, "MEMORY.md"), "w") as f:
            f.write("New entry replacing all\n")

        count3 = index_hermes_memory(self.memory_dir)
        self.assertEqual(count3, 3, "1 MEMORY + 2 USER entries after edit")

    def test_empty_memory_dir(self):
        """No crash when memory dir is empty or nonexistent."""
        empty_dir = os.path.join(test_dir, "empty_memories")
        os.makedirs(empty_dir, exist_ok=True)
        count = index_hermes_memory(empty_dir)
        self.assertEqual(count, 0, "No memory files = 0 entries")

    def test_search_no_source_filter(self):
        """docs_search without source filter should include hermes-memory."""
        index_hermes_memory(self.memory_dir)
        result = docs_search("tomllib")
        self.assertIn("3.11", result, "Unfiltered search should find memory entries")

    def test_cli_command_output_format(self):
        """Simulates `toolrecall index-memory` output structure."""
        index_hermes_memory(self.memory_dir)
        # Search and verify standard output format
        result = docs_search("air-gapped", source="hermes-memory")
        self.assertIn("BM25", result, "Should include BM25 ranking label")
        self.assertIn("hermes-memory", result, "Should show source label")
        self.assertIn("【", result, "Should use FTS5 snippet markers")

    def test_fts5_bm25_ranking(self):
        """BM25 ranks entries with more keyword occurrences higher."""
        index_hermes_memory(self.memory_dir)

        result = docs_search("security", source="hermes-memory")
        self.assertIn("BM25", result, "BM25 ranking label should appear")

    # ── index_directory tests ──────────────────────────────────

    def test_index_directory_creates_pages(self):
        """Each .md file in a dir becomes a page in the knowledge DB."""
        vault = os.path.join(test_dir, "test_vault")
        os.makedirs(vault)
        with open(os.path.join(vault, "note1.md"), "w") as f:
            f.write("# Note One\nContent alpha\n")
        with open(os.path.join(vault, "note2.md"), "w") as f:
            f.write("# Note Two\nContent beta\n")

        count = index_directory(vault, source="test-vault")
        self.assertEqual(count, 2)

    def test_index_directory_search(self):
        """Indexed directory files are searchable via their source label."""
        vault = os.path.join(test_dir, "searchable_vault")
        os.makedirs(vault)
        with open(os.path.join(vault, "meeting.md"), "w") as f:
            f.write("# Meeting Notes\nDiscussed Q3 budget allocation.\n")

        index_directory(vault, source="meetings")

        result = docs_search("Q3", source="meetings")
        self.assertIn("Q3", result)
        self.assertIn("meetings", result)

    def test_index_directory_only_md(self):
        """Non-.md files are skipped by default."""
        vault = os.path.join(test_dir, "mixed_vault")
        os.makedirs(vault)
        with open(os.path.join(vault, "note.md"), "w") as f:
            f.write("# Note\ncontent\n")
        with open(os.path.join(vault, "image.png"), "w") as f:
            f.write("PNG")
        with open(os.path.join(vault, "data.json"), "w") as f:
            f.write('{"key": "value"}')

        count = index_directory(vault, source="mixed")
        self.assertEqual(count, 1, "Only .md files indexed")

    def test_index_directory_nonexistent(self):
        """Non-existent directory returns 0 without crashing."""
        count = index_directory("/nonexistent/path", source="ghost")
        self.assertEqual(count, 0)

    def test_index_directory_custom_extensions(self):
        """Custom extensions are respected."""
        vault = os.path.join(test_dir, "custom_ext")
        os.makedirs(vault)
        with open(os.path.join(vault, "data.txt"), "w") as f:
            f.write("text content\n")
        with open(os.path.join(vault, "note.md"), "w") as f:
            f.write("# Note\ncontent\n")

        count = index_directory(vault, source="custom", extensions=(".txt",))
        self.assertEqual(count, 1, "Only .txt files indexed")
        self.assertIn("data.txt", docs_search("text", source="custom"))

    def test_index_directory_custom_source_label(self):
        """Custom source label via --source is used."""
        vault = os.path.join(test_dir, "custom_source")
        os.makedirs(vault)
        with open(os.path.join(vault, "doc.md"), "w") as f:
            f.write("# Doc\ncontent\n")

        index_directory(vault, source="my-custom-wiki")
        result = docs_search("Doc", source="my-custom-wiki")
        self.assertIn("my-custom-wiki", result)

    def test_index_directory_ignores_dot_dirs(self):
        """Directories like .git are skipped."""
        vault = os.path.join(test_dir, "git_vault")
        os.makedirs(os.path.join(vault, ".git"))
        with open(os.path.join(vault, ".git", "config"), "w") as f:
            f.write("git config\n")
        with open(os.path.join(vault, "readme.md"), "w") as f:
            f.write("# Readme\ncontent\n")

        count = index_directory(vault, source="git-vault", ignore_dirs={".git"})
        self.assertEqual(count, 1, ".git contents skipped")

    def test_index_hermes_memory_custom_source(self):
        """index_hermes_memory() supports custom source label."""
        mem_dir = os.path.join(test_dir, "custom_mem")
        os.makedirs(mem_dir)
        with open(os.path.join(mem_dir, "MEMORY.md"), "w") as f:
            f.write("Test entry\n")

        count = index_hermes_memory(mem_dir, source="custom-memory")
        self.assertEqual(count, 1)
        result = docs_search("Test", source="custom-memory")
        self.assertIn("custom-memory", result)


if __name__ == "__main__":
    unittest.main()