# Copyright (c) 2026 Robin Schultka
# SPDX-License-Identifier: MIT
# Source: https://github.com/whiskybeer/toolrecall
"""toolrecall.adapters.antigravity_hooks — lifecycle hook adapter for Google Antigravity.

Provides deterministic safety, drift detection, and context accuracy for
Antigravity 2.0, Antigravity IDE, and Antigravity CLI (agy) sessions:

1. Workstation Security & WAF Gate (PreToolUse):
   Inspects file operations (`read_file`, `write_file`, `edit_file`) and shell
   executions (`run_command`). Blocks unauthorized access to sensitive files
   (`.env`, SSH keys) and dangerous commands (fork bombs, `rm -rf /`, unverified
   remote shell executions).

2. Context Staleness & Out-of-Band Drift Alerts (PostToolUse + PreInvocation):
   Tracks files read into context. When a shell command modifies files out-of-band
   (e.g. `git checkout`, codegen, linters, or parallel subagent edits), detects
   the drift and injects a high-priority system reminder via PreInvocation so
   the agent re-reads the fresh content before attempting edits.

3. Session Lifecycle Management (SessionStart):
   Isolates state per conversation ID, cleaning up session artifacts on reset.

Configuration in .agents/hooks.json or ~/.gemini/config/hooks.json:
  Run `toolrecall hook antigravity print-config` to generate the configuration block.
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
_FILE_TOOLS = {"read_file", "write_file", "edit_file"}
_COMMAND_TOOLS = {"run_command"}

_BLOCKED_COMMAND_PATTERNS = [
    re.compile(r"\brm\s+-[a-zA-Z]*r[a-zA-Z]*f\s+/(?:\s|$|\*)"),
    re.compile(r"\b(curl|wget)\s+[^|&;]+\|\s*(sh|bash)\b"),
    re.compile(r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:"),  # fork bomb
]
_SENSITIVE_FILES = {".env", ".env.local", ".env.production", "id_rsa", "id_ed25519"}


def _state_dir() -> Path:
    d = os.environ.get("TOOLRECALL_AGY_HOOK_DIR") or os.path.join("~", ".toolrecall", "antigravity")
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
    state: Dict[str, Any] = {"reads": {}, "alerted": {}, "stale": []}
    if p.exists():
        try:
            loaded = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                state = loaded
            state.setdefault("reads", {})
            state.setdefault("alerted", {})
            state.setdefault("stale", [])
        except (OSError, json.JSONDecodeError):
            state = {"reads": {}, "alerted": {}, "stale": []}
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
    """Return (mtime, size, sha256_digest) or None if unreadable/binary/nonexistent."""
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
    stale_paths: List[str] = []
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
                        last_alert = alerted.get(path, 0)
                        if meta.get("mtime") > last_alert or st.st_mtime > last_alert:
                            stale_paths.append(path)
                            alerted[path] = now
        except OSError:
            continue
    return stale_paths


def _check_daemon_stale() -> List[str]:
    """Query ToolRecall daemon for stale files if daemon is reachable."""
    try:
        from toolrecall.client import context_get_stale, daemon_running

        if daemon_running():
            res = context_get_stale()
            return [e["path"] for e in res.get("stale", []) if "path" in e]
    except Exception:
        pass
    return []


def _check_waf_violation(tool_name: str, args: Dict[str, Any]) -> Optional[str]:
    """Check if file access or shell execution violates security policy."""
    # Enabled by default; disable via TOOLRECALL_AGY_WAF=0
    waf_setting = os.environ.get("TOOLRECALL_AGY_WAF", "1").strip().lower()
    if waf_setting in ("0", "false", "no", "off", "disable"):
        return None

    # 1. Path check for sensitive files
    raw_path = args.get("Path") or args.get("path") or args.get("file_path")
    if isinstance(raw_path, str) and raw_path:
        clean_path = raw_path.strip().strip("'\"")
        basename = os.path.basename(clean_path)
        parts = clean_path.split(os.sep)
        if basename in _SENSITIVE_FILES or any(s in parts for s in _SENSITIVE_FILES):
            return f"Blocked access to sensitive file: {basename}"

    # 2. Command inspection
    if tool_name == "run_command":
        cmd = str(args.get("CommandLine") or args.get("command") or "")
        for pattern in _BLOCKED_COMMAND_PATTERNS:
            if pattern.search(cmd):
                return f"Blocked dangerous command pattern: {pattern.pattern}"

    return None


def handle(event: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Primary Antigravity hook event dispatcher.

    Returns dict matching Antigravity hook response schema, or None.
    """
    if not isinstance(event, dict):
        return None

    event_name = event.get("hookEventName") or event.get("hook_event_name") or ""
    session_id = str(
        event.get("conversationId")
        or event.get("conversation_id")
        or event.get("sessionId")
        or event.get("session_id")
        or "default"
    )
    cwd = str(event.get("cwd") or os.getcwd())

    # Reset state on session startup
    if event_name == "SessionStart":
        sdir = _state_dir()
        p = sdir / f"{_safe_session(session_id)}.json"
        try:
            if p.exists():
                p.unlink()
        except OSError:
            pass
        return {}

    # PreToolUse: Workstation WAF Gate
    if event_name == "PreToolUse":
        tool_call = event.get("toolCall") or event.get("tool_call") or {}
        tool_name = tool_call.get("name") or event.get("tool_name") or ""
        args = tool_call.get("args") or event.get("tool_input") or {}

        violation = _check_waf_violation(tool_name, args)
        if violation:
            return {
                "decision": "deny",
                "reason": f"ToolRecall WAF: {violation}",
            }
        return {"decision": "allow"}

    # PostToolUse: Context Tracker Staleness & Drift Alerts
    if event_name == "PostToolUse":
        tool_call = event.get("toolCall") or event.get("tool_call") or {}
        tool_name = tool_call.get("name") or event.get("tool_name") or ""
        args = tool_call.get("args") or event.get("tool_input") or {}

        # 1. File operations: record new baseline content
        if tool_name in _FILE_TOOLS:
            raw_path = args.get("Path") or args.get("path")
            if isinstance(raw_path, str) and raw_path:
                clean_path = raw_path.strip().strip("'\"")
                full_path = os.path.realpath(os.path.join(cwd, os.path.expanduser(clean_path)))
                info = _file_info(full_path)
                if info is not None:
                    mtime, size, digest = info
                    with _session_state(session_id) as st:
                        st["reads"][full_path] = {
                            "mtime": mtime,
                            "size": size,
                            "digest": digest,
                            "read_at": time.time(),
                        }
                        st["alerted"].pop(full_path, None)
                        if full_path in st["stale"]:
                            st["stale"].remove(full_path)
            return {}

        # 2. Shell command execution: check for out-of-band disk modifications
        if tool_name in _COMMAND_TOOLS:
            stale_files: List[str] = []
            with _session_state(session_id) as st:
                stale_files = _check_stale_files(st["reads"], st["alerted"])

                # Optional merge from daemon global stale tracker
                if os.environ.get("TOOLRECALL_AGY_DAEMON_STALE") in ("1", "true", "yes"):
                    for stale_path in _check_daemon_stale():
                        if stale_path not in stale_files:
                            stale_files.append(stale_path)

                if stale_files:
                    for sf in stale_files:
                        if sf not in st["stale"]:
                            st["stale"].append(sf)
            return {}

        return {}

    # PreInvocation: Inject staleness warnings before the model reasons
    if event_name == "PreInvocation":
        pending_stale: List[str] = []
        with _session_state(session_id) as st:
            pending_stale = list(st.get("stale", []))
            # Clear pending list once prepared for injection
            st["stale"] = []

        if pending_stale:
            file_list = "\n".join(f"  - {f}" for f in pending_stale[:5])
            more = f"\n  ... and {len(pending_stale) - 5} more" if len(pending_stale) > 5 else ""
            alert_text = (
                f"⚠️ TOOLRECALL STALENESS ALERT: {len(pending_stale)} file(s) in your conversation "
                f"context were modified on disk and are now out of date:\n{file_list}{more}\n"
                "Your in-context memory for these files is obsolete. Re-read them before "
                "making further edits to prevent failed patches or syntax errors."
            )
            return {
                "injectSteps": [
                    {
                        "systemMessage": {
                            "systemMessage": alert_text,
                        }
                    }
                ]
            }
        return {}

    return {}


def hooks_config(
    command: str = "toolrecall hook antigravity",
) -> Dict[str, Any]:
    """Generate configuration block for Antigravity hooks.json."""
    h = [{"type": "command", "command": command, "timeout": 15}]
    return {
        "toolrecall": {
            "PreToolUse": [
                {
                    "matcher": "*",
                    "hooks": h,
                }
            ],
            "PostToolUse": [
                {
                    "matcher": "run_command|read_file|write_file|edit_file",
                    "hooks": h,
                }
            ],
            "PreInvocation": h,
            "SessionStart": h,
        }
    }


def main(argv: Optional[List[str]] = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if args and args[0] in ("print-config", "--print-config"):
        cfg = hooks_config()
        print(json.dumps(cfg, indent=2))
        return 0

    try:
        raw_in = sys.stdin.read()
        if not raw_in.strip():
            return 0
        event = json.loads(raw_in)
    except Exception:
        # Fail-open if payload cannot be parsed
        return 0

    res = handle(event)
    if res is not None:
        sys.stdout.write(json.dumps(res))
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
