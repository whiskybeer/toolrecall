"""Tests for toolrecall.normalizer — deterministic cache key normalization."""

import json
from toolrecall.normalizer import (
    normalize_json,
    normalize_tool_args,
    normalize_command,
)


class TestNormalizeJSON:
    """Core JSON normalization — sort keys, strip whitespace, compact output."""

    def test_sorts_dict_keys(self):
        result = normalize_json({"z": 1, "a": 2, "m": 3})
        # Keys in alphabetical order: a, m, z
        parsed = json.loads(result)
        assert list(parsed.keys()) == ["a", "m", "z"]
        assert result == '{"a":2,"m":3,"z":1}'

    def test_strips_string_whitespace(self):
        result = normalize_json({"name": "  hello  "})
        assert result == '{"name":"hello"}'

    def test_strips_nested_whitespace(self):
        result = normalize_json({"data": {"text": "  foo  "}})
        assert result == '{"data":{"text":"foo"}}'

    def test_sorts_primitive_lists(self):
        result = normalize_json({"tags": ["z", "a", "m"]})
        assert json.loads(result)["tags"] == ["a", "m", "z"]

    def test_preserves_nonprimitive_list_order(self):
        """Lists of dicts should NOT be sorted (order may be semantic)."""
        result = normalize_json({"items": [{"id": 2}, {"id": 1}]})
        assert json.loads(result)["items"] == [{"id": 2}, {"id": 1}]

    def test_compact_output(self):
        """No extra whitespace in output."""
        result = normalize_json({"a": 1, "b": 2})
        assert " " not in result
        assert "\n" not in result

    def test_empty_dict(self):
        assert normalize_json({}) == "{}"

    def test_none_value(self):
        result = normalize_json({"key": None})
        assert json.loads(result)["key"] is None

    def test_boolean_values(self):
        result = normalize_json({"active": True, "enabled": False})
        assert json.loads(result) == {"active": True, "enabled": False}

    def test_numeric_values(self):
        result = normalize_json({"count": 42, "ratio": 3.14})
        assert json.loads(result) == {"count": 42, "ratio": 3.14}

    def test_deep_nesting(self):
        obj = {"outer": {"inner": {"deepest": "  value  "}}}
        result = normalize_json(obj)
        assert json.loads(result) == {"outer": {"inner": {"deepest": "value"}}}

    def test_mixed_list_types(self):
        """Mixed-type lists should not crash — sort by str representation."""
        result = normalize_json({"mix": [3, "a", 1, "b"]})
        parsed = json.loads(result)
        # Numeric and string mixed — sorted by str(key)
        assert len(parsed["mix"]) == 4


class TestNormalizeToolArgs:
    """Tool call argument normalization — strips non-semantic keys."""

    def test_strips_timestamp_by_default(self):
        result = normalize_tool_args(
            {"path": "/tmp/file", "timestamp": "2026-07-09T12:00:00"}
        )
        assert json.loads(result) == {"path": "/tmp/file"}

    def test_strips_session_id_by_default(self):
        result = normalize_tool_args(
            {"path": "/tmp/file", "session_id": "abc123"}
        )
        assert json.loads(result) == {"path": "/tmp/file"}

    def test_strips_request_id(self):
        result = normalize_tool_args(
            {"path": "/tmp/file", "request_id": "req-456"}
        )
        assert json.loads(result) == {"path": "/tmp/file"}

    def test_strips_trace_id(self):
        result = normalize_tool_args(
            {"path": "/tmp/file", "trace_id": "trace-789", "span_id": "span-012"}
        )
        assert json.loads(result) == {"path": "/tmp/file"}

    def test_preserves_when_disabled(self):
        result = normalize_tool_args(
            {"path": "/tmp/file", "timestamp": "2026-07-09T12:00:00"},
            strip_noise=False,
        )
        assert "timestamp" in json.loads(result)

    def test_normalizes_values(self):
        """Whitespace stripping and key sorting still apply."""
        result = normalize_tool_args(
            {"flags": ["-l"], "path": "  /tmp  "}
        )
        assert json.loads(result) == {"flags": ["-l"], "path": "/tmp"}

    def test_empty_args(self):
        assert normalize_tool_args({}) == "{}"

    def test_case_sensitive_keys(self):
        """Non-semantic keys are matched case-sensitively (lowercase)."""
        result = normalize_tool_args(
            {"path": "/tmp", "Timestamp": "2026-07-09"}
        )
        # "Timestamp" with capital T is NOT stripped
        assert "Timestamp" in json.loads(result)

    def test_multiple_non_semantic_keys(self):
        args = {
            "path": "/tmp/file",
            "timestamp": "t1",
            "session_id": "s2",
            "request_id": "r3",
            "nonce": "n4",
            "trace_id": "t5",
            "content": "real data",
        }
        result = normalize_tool_args(args)
        parsed = json.loads(result)
        assert parsed == {"content": "real data", "path": "/tmp/file"}

    def test_integration_with_cache_scenario(self):
        """Simulate two slightly different MCP calls hitting the same key."""
        call1 = {
            "owner": "whiskybeer",
            "repo": "toolrecall",
            "timestamp": "2026-07-09T12:00:00",
        }
        call2 = {
            "repo": "toolrecall",
            "owner": "whiskybeer",
            "session_id": "sess-abc",
        }
        assert normalize_tool_args(call1) == normalize_tool_args(call2)


class TestNormalizeCommand:
    """Shell command normalization — whitespace, casing."""

    def test_strips_leading_trailing_whitespace(self):
        assert normalize_command("  ls -la  ") == "ls -la"

    def test_collapses_multiple_spaces(self):
        assert normalize_command("ls   -la") == "ls -la"

    def test_lowercases_command_name(self):
        assert normalize_command("LS -LA") == "ls -LA"
        assert normalize_command("ECHO hello") == "echo hello"

    def test_preserves_argument_case(self):
        """Only the command name is lowercased, not arguments."""
        assert normalize_command("grep -i HelloWorld") == "grep -i HelloWorld"

    def test_empty_string(self):
        assert normalize_command("") == ""

    def test_only_whitespace(self):
        assert normalize_command("   ") == ""

    def test_single_word(self):
        assert normalize_command("  HOSTNAME  ") == "hostname"

    def test_tabs_and_newlines(self):
        assert normalize_command("cat\t/etc/hostname\n") == "cat /etc/hostname"