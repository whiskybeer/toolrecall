"""Tests for the Antigravity setup-writer (toolrecall.cli).

Covers `_prepare_antigravity_config` (merge-preserve, atomic write, corrupt-
config protection, absolute command resolution) and the
`_ensure_agent_integration` wiring (Antigravity detected via ~/.gemini).

Mocking notes (same conventions as test_windows_bugs.py):
- shutil is imported INSIDE the cli functions → patch the stdlib module
  (`shutil.which`), not `toolrecall.cli.shutil`.
- expanduser runs on the module-global os → `toolrecall.cli.os.path.expanduser`
  and the helper's own `os.path.expanduser` resolve to the same os module.
"""

import json
import os
import sys

import pytest

from toolrecall import cli


@pytest.fixture()
def fake_home(monkeypatch, tmp_path):
    """Redirect ~ to a tmp home for the duration of a test."""
    home = tmp_path / "home"
    home.mkdir()

    def _expanduser(p):
        if p.startswith("~/"):
            return str(home / p[2:])
        return p

    monkeypatch.setattr("toolrecall.cli.os.path.expanduser", _expanduser)
    monkeypatch.setattr("os.path.expanduser", _expanduser)
    return home


@pytest.fixture()
def no_binaries(monkeypatch):
    """No hermes/claude/toolrecall binaries on PATH — keeps the integration
    function away from real venv discovery and prompt branches."""
    monkeypatch.setattr("shutil.which", lambda name: None)


# ---- _prepare_antigravity_config -----------------------------------------------


class TestPrepareAntigravityConfig:
    def test_creates_fresh_config(self, tmp_path, monkeypatch):
        monkeypatch.setattr("shutil.which", lambda name: "/usr/local/bin/toolrecall")
        path = str(tmp_path / "config" / "mcp_config.json")

        wrote, info = cli._prepare_antigravity_config(path)

        assert wrote is True
        assert info == "created"
        data = json.loads(open(path).read())
        entry = data["mcpServers"]["toolrecall"]
        assert entry["command"] == "/usr/local/bin/toolrecall"
        assert entry["args"] == ["mcp"]

    def test_parent_dirs_created(self, tmp_path, monkeypatch):
        monkeypatch.setattr("shutil.which", lambda name: None)
        path = str(tmp_path / "a" / "b" / "mcp_config.json")
        wrote, _ = cli._prepare_antigravity_config(path)
        assert wrote is True
        assert os.path.exists(path)

    def test_merges_preserving_user_servers(self, tmp_path, monkeypatch):
        monkeypatch.setattr("shutil.which", lambda name: "/tr/bin/toolrecall")
        path = tmp_path / "mcp_config.json"
        path.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "my-remote": {"serverUrl": "https://api.example.com/mcp/"},
                        "sqlite-explorer": {
                            "command": "node",
                            "args": ["/usr/local/bin/sqlite-mcp.js"],
                        },
                    }
                }
            )
        )

        wrote, info = cli._prepare_antigravity_config(str(path))

        assert wrote is True
        assert info == "updated"
        data = json.loads(path.read_text())
        # user servers untouched, verbatim
        assert data["mcpServers"]["my-remote"] == {"serverUrl": "https://api.example.com/mcp/"}
        assert data["mcpServers"]["sqlite-explorer"]["command"] == "node"
        # toolrecall entry added
        assert data["mcpServers"]["toolrecall"]["args"] == ["mcp"]

    def test_idempotent(self, tmp_path, monkeypatch):
        monkeypatch.setattr("shutil.which", lambda name: "/tr/bin/toolrecall")
        path = str(tmp_path / "mcp_config.json")
        cli._prepare_antigravity_config(path)
        first = open(path).read()
        wrote2, info2 = cli._prepare_antigravity_config(path)
        assert wrote2 is True
        assert info2 == "updated"
        assert open(path).read() == first  # stable, no duplication

    def test_corrupt_config_not_clobbered(self, tmp_path, monkeypatch):
        monkeypatch.setattr("shutil.which", lambda name: None)
        path = tmp_path / "mcp_config.json"
        corrupt = '{"mcpServers": { "broken": '
        path.write_text(corrupt)

        wrote, warn = cli._prepare_antigravity_config(str(path))

        assert wrote is False
        assert "unreadable" in warn
        assert path.read_text() == corrupt  # user file untouched

    def test_mcpServers_not_object_rejected(self, tmp_path, monkeypatch):
        monkeypatch.setattr("shutil.which", lambda name: None)
        path = tmp_path / "mcp_config.json"
        path.write_text(json.dumps({"mcpServers": [1, 2]}))

        wrote, warn = cli._prepare_antigravity_config(str(path))

        assert wrote is False
        assert "not an object" in warn

    def test_no_tmp_files_left_behind(self, tmp_path, monkeypatch):
        monkeypatch.setattr("shutil.which", lambda name: "/tr/bin/toolrecall")
        path = str(tmp_path / "mcp_config.json")
        cli._prepare_antigravity_config(path)
        leftovers = [f for f in os.listdir(tmp_path) if f.startswith(".mcp_config_")]
        assert leftovers == []

    def test_command_falls_back_to_bare_name(self, tmp_path, monkeypatch):
        """which→None and no ~/.local/bin/toolrecall → bare name (client PATH)."""
        monkeypatch.setattr("shutil.which", lambda name: None)
        monkeypatch.setattr(
            "os.path.expanduser",
            lambda p: str(tmp_path / p[2:]) if p.startswith("~/") else p,
        )
        # ensure ~/.local/bin/toolrecall does not exist in the fake home
        path = str(tmp_path / "mcp_config.json")
        wrote, _ = cli._prepare_antigravity_config(path)
        data = json.loads(open(path).read())
        assert data["mcpServers"]["toolrecall"]["command"] == "toolrecall"

    def test_fallback_finds_windows_exe(self, tmp_path, monkeypatch):
        """Windows pipx shim: ~/.local/bin/toolrecall.exe must be picked up
        even when shutil.which cannot see it (GUI PATH)."""
        monkeypatch.setattr("shutil.which", lambda name: None)
        bin_dir = tmp_path / ".local" / "bin"
        bin_dir.mkdir(parents=True)
        exe = bin_dir / "toolrecall.exe"
        exe.write_text("")  # stub binary
        monkeypatch.setattr(
            "os.path.expanduser",
            lambda p: str(tmp_path / p[2:]) if p.startswith("~/") else p,
        )
        cmd = cli._toolrecall_command_for_mcp()
        assert cmd[0] == str(exe)
        assert cmd[1:] == ["mcp"]

    def test_fallback_finds_windows_cmd_shim(self, tmp_path, monkeypatch):
        monkeypatch.setattr("shutil.which", lambda name: None)
        bin_dir = tmp_path / ".local" / "bin"
        bin_dir.mkdir(parents=True)
        shim = bin_dir / "toolrecall.cmd"
        shim.write_text("")
        monkeypatch.setattr(
            "os.path.expanduser",
            lambda p: str(tmp_path / p[2:]) if p.startswith("~/") else p,
        )
        cmd = cli._toolrecall_command_for_mcp()
        assert cmd[0] == str(shim)

    def test_absolute_path_used_when_resolvable(self, tmp_path, monkeypatch):
        monkeypatch.setattr("shutil.which", lambda name: "/opt/pipx/toolrecall")
        path = str(tmp_path / "mcp_config.json")
        cli._prepare_antigravity_config(path)
        data = json.loads(open(path).read())
        assert data["mcpServers"]["toolrecall"]["command"] == "/opt/pipx/toolrecall"


# ---- _toolrecall_command_for_mcp ------------------------------------------------


class TestToolrecallCommandForMcp:
    def test_uses_which_result(self, monkeypatch):
        monkeypatch.setattr("shutil.which", lambda name: "/x/toolrecall")
        assert cli._toolrecall_command_for_mcp() == ["/x/toolrecall", "mcp"]

    def test_windows_style_path(self, monkeypatch):
        """Windows pipx path shape: absolute path must pass through untouched
        (no POSIX assumptions in the helper)."""
        monkeypatch.setattr("shutil.which", lambda name: "C:\\Users\\u\\bin\\toolrecall.exe")
        cmd = cli._toolrecall_command_for_mcp()
        assert cmd[0] == "C:\\Users\\u\\bin\\toolrecall.exe"
        assert cmd[1:] == ["mcp"]


# ---- _ensure_agent_integration wiring ---------------------------------------------


class TestSetupIntegration:
    def test_antigravity_detected_when_gemini_home_exists(
        self, fake_home, no_binaries, monkeypatch
    ):
        (fake_home / ".gemini").mkdir()
        monkeypatch.setattr(cli, "_ensure_daemon", lambda: True)

        result = cli._ensure_agent_integration()

        assert result["antigravity"] is True
        cfg = json.loads((fake_home / ".gemini" / "config" / "mcp_config.json").read_text())
        assert "toolrecall" in cfg["mcpServers"]

    def test_antigravity_skipped_without_gemini_home(self, fake_home, no_binaries, monkeypatch):
        monkeypatch.setattr(cli, "_ensure_daemon", lambda: True)

        result = cli._ensure_agent_integration()

        assert result["antigravity"] is False
        assert not (fake_home / ".gemini" / "config" / "mcp_config.json").exists()

    def test_notice_printed_on_success(self, fake_home, no_binaries, monkeypatch):
        (fake_home / ".gemini").mkdir()
        monkeypatch.setattr(cli, "_ensure_daemon", lambda: True)
        # Spy instead of capsys: capsys is unavailable under -p no:capture
        # (full-suite runs); the spy survives any capture configuration.
        called = []

        real_notice = cli._print_antigravity_notice

        def spy():
            called.append(True)
            real_notice()

        monkeypatch.setattr(cli, "_print_antigravity_notice", spy)
        import io

        real_stdout = sys.stdout
        buf = io.StringIO()
        try:
            sys.stdout = buf
            cli._ensure_agent_integration()
        finally:
            sys.stdout = real_stdout

        assert called == [True]  # notice fired exactly once on success
        out = buf.getvalue()
        assert "replay record" in out
        assert "cached_tool" in out
        assert "mcp_multiplex" in out
