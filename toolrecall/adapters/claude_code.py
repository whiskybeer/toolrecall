# Copyright (c) 2026 Robin Schultka
# SPDX-License-Identifier: MIT
# Source: https://github.com/whiskybeer/toolrecall
"""toolrecall.adapters.claude_code — hook adapter for Claude Code.

Provides two deterministic enhancements for Claude Code sessions:

1. Context Tracker Staleness & Drift Alerts (PostToolUse):
   Claude Code's transcript is append-only from the outside, so external tools
   cannot prune old tool results. However, when an agent executes shell commands
   (e.g. `git checkout`, `sed`, `npm run build`, `make`) or code modifications,
   files previously read into context are modified on disk out-of-band.
   This hook tracks session file reads and, after modifying tools run, checks if
   any in-context file has changed on disk (or is reported stale by the
   ToolRecall daemon's monotonic clock). If drift is detected, it injects
   `additionalContext` alerting Claude to re-read the fresh file before editing.

2. Workstation Security & WAF Gate (PreToolUse):
   When enabled (`TOOLRECALL_CC_WAF=1`), inspects `Bash`, `Write`, and `Edit`
   payloads. Blocks access to `.env` air-gapped secrets and dangerous command
   patterns (e.g. `rm -rf /`, unverified remote shell execution).

Configuration in ~/.claude/settings.json or .claude/settings.json:
  Run `python3 -m toolrecall.adapters.claude_code print-config` to generate
  the settings snippet.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from toolrecall.keys import content_digest

try:  # POSIX only; without it concurrent hooks may lose an update (fail-open).
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]

_SKIP_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".pdf", ".ipynb"}
_EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
_BLOCKED_COMMAND_PATTERNS = [
    re.compile(r"\brm\s+-[a-zA-Z]*r[a-zA-Z]*f\s+/(?:\s|$|\*)"),
    re.compile(r"\b(curl|wget)\s+[^|&;]+\|\s*(sh|bash)\b"),
    re.compile(r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:"),  # fork bomb
]
_SENSITIVE_FILES = {".env", ".env.local", ".env.production", "id_rsa", "id_ed25519"}


def _state_dir() -> Path:
    d = os.environ.get("TOOLRECALL_CC_HOOK_DIR") or os.path.join("~", ".toolrecall", "claude-code")
    return Path(os.path.expanduser(d))


def _safe_session(session_id: str) -> str:
    return "".join(c for c in session_id if c.isalnum() or c in "-_")[:128] or "default"


@contextmanager
def _session_state(session_id: str):
    sdir = _state_dir()
    sdir.mkdir(parents=True, exist_ok=True)
    p = sdir / f"{_safe_session(session_id)}.json"
    lock_p = sdir / f"{_safe_session(session_id)}.lock"
    lock_fd = None
    if fcntl:
        try:
            lock_fd = os.open(str(lock_p), os.O_CREAT | os.O_RDWR, 0o600)
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
        except OSError:
            lock_fd = None
    state: Dict[str, Any] = {"reads": {}, "alerted": {}}
    if p.exists():
        try:
            state = json.loads(p.read_text(encoding="utf-8"))
            if not isinstance(state, dict):
                state = {"reads": {}, "alerted": {}}
            state.setdefault("reads", {})
            state.setdefault("alerted", {})
        except (OSError, json.JSONDecodeError):
            state = {"reads": {}, "alerted": {}}
    try:
        yield state
        tmp = sdir / f"{p.name}.tmp.{os.getpid()}"
        tmp.write_text(json.dumps(state), encoding="utf-8")
        os.replace(str(tmp), str(p))
    finally:
        if fcntl and lock_fd is not None:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
                os.close(lock_fd)
            except OSError:
                pass


def _file_info(path: str) -> Optional[Tuple[float, int, str]]:
    """Return (mtime, size, sha256_digest) or None if unreadable/binary."""
    if any(path.endswith(s) for s in _SKIP_SUFFIXES):
        return None
    try:
        st = os.stat(path)
        data = Path(path).read_bytes()
        digest = content_digest(data)
        return float(st.st_mtime), int(st.st_size), digest
    except OSError:
        return None


def _check_stale_files(reads: Dict[str, Any], alerted: Dict[str, Any]) -> List[str]:
    """Identify files in `reads` whose content on disk has diverged."""
    stale_paths = []
    now = time.time()
    for path, meta in list(reads.items()):
        if not os.path.exists(path):
            continue
        try:
            st = os.stat(path)
            # Fast check: mtime or size change
            if st.st_mtime != meta.get("mtime") or st.st_size != meta.get("size"):
                info = _file_info(path)
                if info is not None:
                    _, _, current_digest = info
                    if current_digest != meta.get("digest"):
                        # Content really changed on disk!
                        last_alert = alerted.get(path, 0)
                        # Alert if not yet alerted for this modification
                        if meta.get("mtime") > last_alert or st.st_mtime > last_alert:
                            stale_paths.append(path)
                            alerted[path] = now
        except OSError:
            continue
    return stale_paths


def _check_daemon_stale() -> List[str]:
    """Query ToolRecall daemon for stale files if daemon is reachable."""
    try:
        from toolrecall.client import context_get_stale, is_healthy

        if is_healthy(timeout=0.1):
            res = context_get_stale()
            return [e["path"] for e in res.get("stale", []) if "path" in e]
    except Exception:
        pass
    return []


def _check_waf_violation(tool_name: str, tool_input: Dict[str, Any]) -> Optional[str]:
    """Check if command/edit violates security policy."""
    if os.environ.get("TOOLRECALL_CC_WAF") not in ("1", "true", "enforce"):
        return None

    # 1. Path check for sensitive files
    path = tool_input.get("file_path") or tool_input.get("path")
    if isinstance(path, str):
        basename = os.path.basename(path)
        if basename in _SENSITIVE_FILES or any(s in path.split(os.sep) for s in _SENSITIVE_FILES):
            return f"Blocked access to sensitive file: {basename}"

    # 2. Bash command inspection
    if tool_name == "Bash":
        cmd = str(tool_input.get("command") or "")
        for pattern in _BLOCKED_COMMAND_PATTERNS:
            if pattern.search(cmd):
                return f"Blocked dangerous command pattern: {pattern.pattern}"

    return None


def handle(event: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Primary hook event dispatcher.

    Returns dict matching Claude Code hook response schema, or None.
    """
    if not isinstance(event, dict):
        return None

    name = event.get("hook_event_name")
    tool = event.get("tool_name") or ""
    sid = str(event.get("session_id") or "default")
    tin = event.get("tool_input") or {}
    cwd = str(event.get("cwd") or "")

    # Reset state on session lifecycle boundaries
    if name in ("SessionStart", "PreCompact"):
        sdir = _state_dir()
        p = sdir / f"{_safe_session(sid)}.json"
        try:
            if p.exists():
                p.unlink()
        except OSError:
            pass
        return None

    # PreToolUse: Workstation WAF Gate
    if name == "PreToolUse":
        violation = _check_waf_violation(tool, tin)
        if violation:
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": f"ToolRecall WAF: {violation}",
                }
            }
        return None

    # PostToolUse: Context Tracker Staleness & Drift Alerts
    if name == "PostToolUse":
        # 1. Reads and the agent's own writes: the file's current content is what
        #    Claude has in context, so record it as the fresh baseline. An agent
        #    edit must never be reported as drift (Claude Code itself tells the
        #    model "file state is current in your context" after an Edit).
        if tool == "Read" or tool in _EDIT_TOOLS:
            raw_path = tin.get("file_path") or tin.get("notebook_path")
            if isinstance(raw_path, str) and raw_path:
                full_path = os.path.realpath(os.path.join(cwd or ".", os.path.expanduser(raw_path)))
                info = _file_info(full_path)
                if info is not None:
                    mtime, size, digest = info
                    with _session_state(sid) as st:
                        st["reads"][full_path] = {
                            "mtime": mtime,
                            "size": size,
                            "digest": digest,
                            "read_at": time.time(),
                        }
                        # Clear any stale alert for this path: Claude has it fresh again
                        st["alerted"].pop(full_path, None)
            return None

        # 2. Out-of-band drift can only come from shell commands
        if tool == "Bash":
            stale_files: List[str] = []
            with _session_state(sid) as st:
                stale_files = _check_stale_files(st["reads"], st["alerted"])

            # The daemon's stale list is global across agents (it tracks reads made
            # through ToolRecall's MCP tools, not this session's Read calls), so
            # merging it can surface files this session never saw. Opt-in only.
            if os.environ.get("TOOLRECALL_CC_DAEMON_STALE") in ("1", "true", "yes"):
                for p in _check_daemon_stale():
                    if p not in stale_files:
                        stale_files.append(p)

            if stale_files:
                file_list = "\n".join(f"  - {f}" for f in stale_files[:5])
                more = f"\n  ... and {len(stale_files) - 5} more" if len(stale_files) > 5 else ""
                alert_text = (
                    f"⚠️ TOOLRECALL STALENESS ALERT: {len(stale_files)} file(s) in your conversation "
                    f"context were modified on disk and are now out of date:\n{file_list}{more}\n"
                    "Your in-context memory for these files is obsolete. Re-read them before "
                    "making further edits to prevent failed patches or syntax errors."
                )
                return {
                    "hookSpecificOutput": {
                        "hookEventName": "PostToolUse",
                        "additionalContext": alert_text,
                    }
                }

    return None


def hooks_config(command: str = "python3 -m toolrecall.adapters.claude_code") -> Dict[str, Any]:
    """Generate configuration block for settings.json."""
    h = [{"type": "command", "command": command, "timeout": 15}]
    return {
        "hooks": {
            "PreToolUse": [{"matcher": "Bash|Write|Edit|MultiEdit", "hooks": h}],
            "PostToolUse": [{"matcher": "Read|Bash|Edit|Write|MultiEdit|NotebookEdit", "hooks": h}],
            "PreCompact": [{"hooks": h}],
            "SessionStart": [{"matcher": "compact|clear", "hooks": h}],
        }
    }


def ci_env(port: int = 8569) -> str:
    """Shell snippet for repeated headless runs (CI re-runs, eval/benchmark iteration).

    This is the one measured way ToolRecall saves tokens for Claude Code:
    forward-proxy replay of a task that runs again on the same commit.
    """
    return "\n".join(
        [
            "# ToolRecall replay for repeated headless Claude Code runs (source this file).",
            "# Saves only when the SAME task runs again on the SAME commit with deterministic",
            "# tool output. A new commit changes the first request (no replay). Do not use",
            "# for evals that sample the model: a replayed call returns the earlier answer.",
            f"export ANTHROPIC_BASE_URL=http://127.0.0.1:{port}",
            'export ANTHROPIC_CUSTOM_HEADERS="X-ToolRecall-Canonicalize: claude-code"',
            "export PYTHONHASHSEED=0  # stable set/dict order in Python tool output",
            "# Pin MCP servers: connector tools load asynchronously and change the tool list.",
            "TR_CLAUDE_FLAGS=(--strict-mcp-config --mcp-config '{\"mcpServers\":{}}')",
            '# usage: claude -p "<task>" "${TR_CLAUDE_FLAGS[@]}"',
            "# Why did replay stop? Start the proxy with TOOLRECALL_KEY_TRACE_DIR=<dir>,",
            "# then run: toolrecall keytrace <dir>",
        ]
    )


def main(argv: Optional[List[str]] = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["ci-env"]:
        port = int(argv[2]) if len(argv) > 2 and argv[1] == "--port" else 8569
        print(ci_env(port))
        return 0
    if argv[:1] == ["print-config"]:
        cmd = argv[1] if len(argv) > 1 else "python3 -m toolrecall.adapters.claude_code"
        print(json.dumps(hooks_config(cmd), indent=2))
        return 0

    try:
        payload = json.load(sys.stdin)
        result = handle(payload)
        if result is not None:
            print(json.dumps(result))
    except Exception:
        # Hard fail-open: hooks must never crash or block agent workflow on unexpected errors
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
