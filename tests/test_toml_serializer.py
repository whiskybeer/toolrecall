"""Tests for toolrecall.toml_serializer — zero-deps TOML writer.

Covers: scalars, lists, nested sections, inline tables, special strings,
error cases, round-trip with real config.
"""
import pytest
import datetime
from toolrecall.toml_serializer import dumps, dump, format_section, _escape_basic, _format_value


class TestEscapeBasic:
    """_escape_basic(): string escaping per TOML spec."""

    def test_plain_string(self):
        assert _escape_basic("hello") == '"hello"'

    def test_contains_double_quote(self):
        assert _escape_basic('say "hi"') == r'"say \"hi\""'

    def test_contains_backslash(self):
        assert _escape_basic(r"a\b") == r'"a\\b"'

    def test_newline(self):
        assert _escape_basic("a\nb") == r'"a\nb"'

    def test_tab(self):
        assert _escape_basic("a\tb") == r'"a\tb"'

    def test_control_char(self):
        assert _escape_basic("a\x00b") == r'"a\u0000b"'

    def test_unicode(self):
        s = _escape_basic("café")
        assert s == '"café"'


class TestFormatValue:
    """_format_value(): value formatting for all TOML types."""

    def test_none(self):
        assert _format_value(None) == ""

    def test_bool_true(self):
        assert _format_value(True) == "true"

    def test_bool_false(self):
        assert _format_value(False) == "false"

    def test_int(self):
        assert _format_value(42) == "42"
        assert _format_value(-7) == "-7"

    def test_int_zero(self):
        assert _format_value(0) == "0"

    def test_float(self):
        assert _format_value(3.14) == "3.14"

    def test_float_special(self):
        assert _format_value(float("inf")) == "inf"
        assert _format_value(float("-inf")) == "-inf"
        assert _format_value(float("nan")) == "nan"

    def test_string(self):
        assert _format_value("hello") == '"hello"'

    def test_date(self):
        d = datetime.date(2026, 7, 4)
        assert _format_value(d) == "2026-07-04"

    def test_datetime(self):
        dt = datetime.datetime(2026, 7, 4, 14, 30, 0)
        assert _format_value(dt) == "2026-07-04T14:30:00"

    def test_list_empty(self):
        assert _format_value([]) == "[]"

    def test_list_compact(self):
        assert _format_value([1, 2, 3]) == "[1, 2, 3]"

    def test_list_multiline(self):
        result = _format_value([1, 2, 3, 4, 5, 6])
        assert "[\n" in result
        assert "5" in result

    def test_list_mixed(self):
        result = _format_value(["a", "b", "c", "d", "e", "f"])
        assert "[\n" in result

    def test_inline_table(self):
        from toolrecall.toml_serializer import _format_inline_table
        result = _format_inline_table({"a": 1, "b": 2})
        assert "{" in result
        assert "1" in result
        assert "2" in result

    def test_unsupported_type_raises(self):
        with pytest.raises(TypeError):
            _format_value(object())


class TestDumps:
    """dumps(): dict to TOML string."""

    def test_empty_dict(self):
        assert dumps({}).strip() == ""

    def test_bare_keys(self):
        result = dumps({"title": "ToolRecall", "version": 42})
        assert result.strip().startswith('title = "ToolRecall"')
        assert 'version = 42' in result

    def test_single_section(self):
        result = dumps({"storage": {"backend": "sqlite"}})
        assert "[storage]" in result
        assert 'backend = "sqlite"' in result

    def test_nested_section(self):
        result = dumps({"mcp": {"security": {"enabled": True}}})
        assert "[mcp.security]" in result
        assert "enabled = true" in result

    def test_list_of_strings(self):
        result = dumps({"mcp": {"allowed_paths": ["~/a", "~/b"]}})
        assert "[mcp]" in result
        assert "allowed_paths" in result
        assert '"~/a"' in result

    def test_bool_values(self):
        result = dumps({"mcp": {"allow_terminal": False}})
        assert "false" in result

    def test_empty_section(self):
        result = dumps({"empty": {}})
        assert "[empty]" in result

    def test_empty_list(self):
        result = dumps({"mcp": {"deps": [], "servers": []}})
        assert "deps = []" in result
        assert "servers = []" in result

    def test_comment(self):
        result = dumps({"storage": {"backend": "sqlite"}}, comment="My config")
        assert "# My config" in result

    def test_multiline_list(self):
        data = {"servers": ["alpha", "beta", "gamma", "delta", "epsilon", "zeta"]}
        result = dumps(data)
        assert "[\n" in result

    def test_full_config_roundtrip(self):
        """Full config matching real toolrecall.toml."""
        data = {
            "storage": {"backend": "sqlite"},
            "cache": {
                "file_ttl": -1,
                "terminal_default_ttl": 300,
            },
            "mcp": {
                "allowed_paths": ["~/projects", "~/.hermes/skills"],
                "allow_terminal": False,
            },
        }
        text = dumps(data)
        assert "[storage]" in text
        assert "[cache]" in text
        assert "[mcp]" in text
        assert 'backend = "sqlite"' in text
        assert "file_ttl = -1" in text
        assert "terminal_default_ttl = 300" in text
        assert "allow_terminal = false" in text
        assert "allowed_paths" in text
        assert '"~/projects"' in text


class TestDump:
    """dump(): write to file-like object."""

    def test_dump_writes_to_file(self, tmp_path):
        f = tmp_path / "test.toml"
        with open(f, "w") as fh:
            dump({"storage": {"backend": "test"}}, fh)
        content = f.read_text()
        assert "[storage]" in content
        assert 'backend = "test"' in content

    def test_dump_with_comment(self, tmp_path):
        f = tmp_path / "comment.toml"
        with open(f, "w") as fh:
            dump({"a": 1}, fh, comment="Top comment")
        content = f.read_text()
        assert "# Top comment" in content

    def test_dump_overwrites(self, tmp_path):
        f = tmp_path / "overwrite.toml"
        f.write_text("old = 1")
        with open(f, "w") as fh:
            dump({"version": 2}, fh)
        content = f.read_text()
        assert "version = 2" in content
        assert "old" not in content


class TestFormatSection:
    """format_section(): section header + key-value pairs."""

    def test_simple_section(self):
        result = format_section("storage", {"backend": "sqlite"})
        assert result.startswith("[storage]")
        assert 'backend = "sqlite"' in result

    def test_section_with_subsection(self):
        result = format_section("mcp", {"allowed_paths": ["~/a"], "security": {"token": "x"}})
        assert "[mcp]" in result
        assert "[mcp.security]" in result

    def test_section_mixed(self):
        result = format_section("cache", {"ttl": 60, "ttls": {"whoami": 300}})
        assert "[cache]" in result
        assert "ttl = 60" in result


class TestRealConfig:
    """Regression: write a real ToolRecall config and verify it parses back."""

    def test_roundtrip_via_tomllib(self, tmp_path):
        """Write via our serializer, read back via stdlib tomllib."""
        data = {
            "storage": {"backend": "sqlite"},
            "cache": {
                "file_ttl": -1,
                "terminal_default_ttl": 300,
            },
            "mcp": {
                "allowed_paths": ["~/projects"],
                "allow_terminal": True,
                "allow_invalidate": False,
            },
            "mcp_multiplex": {
                "enabled": True,
                "servers": ["time", "github"],
                "default_ttl": 60,
            },
            "security": {
                "tool_access_control": False,
                "dangerous_tool_keywords": [],
            },
        }

        f = tmp_path / "config.toml"
        with open(f, "w") as fh:
            dump(data, fh)

        import tomllib
        with open(f, "rb") as fh:
            parsed = tomllib.load(fh)

        assert parsed["storage"]["backend"] == "sqlite"
        assert parsed["cache"]["file_ttl"] == -1
        assert parsed["cache"]["terminal_default_ttl"] == 300
        assert parsed["mcp"]["allowed_paths"] == ["~/projects"]
        assert parsed["mcp"]["allow_terminal"] is True
        assert parsed["mcp"]["allow_invalidate"] is False
        assert parsed["mcp_multiplex"]["enabled"] is True
        assert parsed["mcp_multiplex"]["servers"] == ["time", "github"]
        assert parsed["mcp_multiplex"]["default_ttl"] == 60
        assert parsed["security"]["tool_access_control"] is False

    def test_empty_file_parses(self, tmp_path):
        f = tmp_path / "empty.toml"
        with open(f, "w") as fh:
            dump({}, fh)
        import tomllib
        with open(f, "rb") as fh:
            parsed = tomllib.load(fh)
        assert parsed == {}
