# Zero-dependency TOML serializer — writes TOML without tomli-w.
# Replaces tomli-w dependency entirely. Pure Python, stdlib only.
#
# Usage:
#     from toolrecall.toml_serializer import dumps, dump
#
#     text = dumps({"mcp": {"allowed_paths": ["~/projects"], "allow_terminal": True}})
#     # -> '[mcp]\nallowed_paths = [\"~/projects\"]\nallow_terminal = true\n'
#
#     with open("config.toml", "w") as f:
#         dump({"storage": {"backend": "sqlite"}}, f)
#
# Supports: int, str, float, bool, None, list, dict, datetime.date
#
# Does NOT support:
#   - Inline tables (foo = {bar = "baz"}) — use section headers instead
#   - Array of tables [[foo]] — use plain sections
#   - Multi-line strings ("""...""") — plain strings only
#
# For ToolRecall's use case (writing config.toml) this is sufficient.

import datetime


def _escape_basic(s: str) -> str:
    """Escape a basic string per TOML spec."""
    result = []
    for ch in s:
        if ch == "\n":
            result.append("\\n")
        elif ch == "\r":
            result.append("\\r")
        elif ch == "\t":
            result.append("\\t")
        elif ch == '"':
            result.append('\\"')
        elif ch == "\\":
            result.append("\\\\")
        elif ord(ch) < 0x20:
            result.append(f"\\u{ord(ch):04x}")
        else:
            result.append(ch)
    return '"' + "".join(result) + '"'


def _format_value(val, indent: int = 0) -> str:
    """Format a TOML value with proper indentation."""
    pad = "  " * indent

    if val is None:
        return ""
    if isinstance(val, bool):
        return "true" if val else "false"
    if isinstance(val, int):
        return str(val)
    if isinstance(val, float):
        if val == float("inf"):
            return "inf"
        if val == float("-inf"):
            return "-inf"
        if val != val:  # NaN
            return "nan"
        return repr(val)
    if isinstance(val, str):
        return _escape_basic(val)
    if isinstance(val, datetime.date):
        if isinstance(val, datetime.datetime):
            return val.isoformat()
        return val.isoformat()
    if isinstance(val, list):
        return _format_list(val, indent)
    if isinstance(val, dict):
        return _format_inline_table(val, indent)

    raise TypeError(f"Unsupported TOML type: {type(val).__name__}")


def _format_list(val: list, indent: int = 0) -> str:
    """Format a list as TOML array."""
    if not val:
        return "[]"

    all_simple = all(
        isinstance(v, (str, int, float, bool, type(None)))
        for v in val
    )

    if all_simple and len(val) <= 5:
        items = ", ".join(_format_value(v) for v in val)
        return f"[{items}]"
    else:
        pad = "  " * (indent + 1)
        items = [_format_value(v, indent + 1) for v in val]
        inner = ",\n".join(f"{pad}{item}" for item in items)
        close_pad = "  " * indent
        return f"[\n{inner},\n{close_pad}]"


def _format_inline_table(val: dict, indent: int = 0) -> str:
    """Format a dict as an inline TOML table.
    Only used for values inside lists. Top-level sections use format_section().
    """
    if not val:
        return "{}"
    items = []
    for k, v in val.items():
        k_str = _escape_basic(k) if "-" in k or " " in k else k
        items.append(f"{k_str} = {_format_value(v, indent)}")
    return "{ " + ", ".join(items) + " }"


def format_section(section_name: str, data: dict, indent: int = 0) -> str:
    """Format a section heading + key-value pairs.

    Args:
        section_name: Section heading without brackets, e.g. 'mcp' or 'mcp.multiplex'
        data: Key-value pairs for this section
        indent: Indentation level (for nested sections)

    Returns:
        TOML string: "[section_name]\nkey = value\n..."
    """
    header = f"[{section_name}]"
    lines = [header]

    plain_keys = {}
    sub_sections = {}

    for k, v in data.items():
        if isinstance(v, dict):
            # Any nested dict becomes a sub-section
            sub_sections[k] = v
        elif isinstance(v, list) and v and all(isinstance(item, dict) for item in v):
            # Array of tables — each item becomes its own section
            sub_sections[k] = v
        else:
            plain_keys[k] = v

    for k, v in plain_keys.items():
        k_str = _escape_basic(k) if "-" in k or " " in k else k
        if isinstance(v, dict):
            sub_lines = []
            for sk, sv in v.items():
                sv_str = _escape_basic(sk) if "-" in sk or " " in sk else sk
                sub_lines.append(f"  {sv_str} = {_format_value(sv, indent + 1)}")
            inner = ",\n".join(sub_lines)
            pad = "  " * (indent + 1)
            lines.append(f"{k_str} = {{\n{inner},\n{pad}}}")
        else:
            lines.append(f"{k_str} = {_format_value(v, indent)}")

    for k, v in sub_sections.items():
        if isinstance(v, dict):
            child_name = f"{section_name}.{k}" if section_name else k
            lines.append("")
            lines.append(format_section(child_name, v, indent))
        elif isinstance(v, list):
            for item in v:
                if isinstance(item, dict):
                    child_name = f"{section_name}.{k}" if section_name else k
                    lines.append("")
                    lines.append(format_section(child_name, item, indent))

    return "\n".join(lines)


def _is_inline_compatible(d: dict) -> bool:
    """Check if a dict can be rendered as an inline table (only simple values)."""
    for v in d.values():
        if isinstance(v, (dict, list)):
            return False
    return True


def dumps(data: dict, comment: str = None) -> str:
    """Serialize a dict to TOML string."""
    lines = []
    if comment:
        for line in comment.split("\n"):
            lines.append(f"# {line}")
        lines.append("")

    sections = {}
    bare_keys = {}

    for k, v in data.items():
        if isinstance(v, dict):
            # Top-level dicts are always sections, never inline
            sections[k] = v
        else:
            bare_keys[k] = v

    for k, v in bare_keys.items():
        k_str = _escape_basic(k) if "-" in k or " " in k else k
        lines.append(f"{k_str} = {_format_value(v)}")
    if bare_keys:
        lines.append("")

    for section_name, section_data in sections.items():
        lines.append(format_section(section_name, section_data))
        lines.append("")

    return "\n".join(lines)


def dump(data: dict, file, comment: str = None):
    """Serialize a dict to TOML and write to a file-like object."""
    text = dumps(data, comment=comment)
    file.write(text)


if __name__ == "__main__":
    data = {
        "storage": {"backend": "sqlite"},
        "cache": {
            "file_ttl": -1,
            "terminal_default_ttl": 300,
            "terminal_ttls": {"whoami": 600, "hostname": 3600},
        },
        "mcp": {
            "allowed_paths": ["~/projects", "~/.hermes/skills"],
            "allow_terminal": False,
            "allowed_terminal_commands": [],
        },
        "mcp_multiplex": {
            "enabled": True,
            "servers": ["time", "github", "sequential-thinking"],
            "default_ttl": 60,
            "idle_minutes": 15,
        },
        "security": {
            "tool_access_control": False,
            "dangerous_tool_keywords": [],
        },
        "forward_proxy": {
            "port": 8569,
        },
    }
    text = dumps(data, comment="ToolRecall Configuration")
    print(text)
