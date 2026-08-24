"""ToolRecall daemon/cache healthcheck — generalized, self-contained.

Implements a stable, parameterized healthcheck that any operator can run
directly (`toolrecall healthcheck`) or wrap in a cron/systemd watchdog. It
reuses ToolRecall's **own** path helpers (`transport._default_socket_path`,
`daemon.PID_FILE`, `daemon._instance_lock_path`) so it always checks the same
files the daemon actually uses — no hardcoded per-machine paths.

Reported signals:
    procs        number of live `toolrecall daemon` processes (-1 unknown)
    pid_file     pid read from the daemon pid file ('' if absent)
    pid_alive    whether that pid maps to a live process
    lock_files   count of ~/.toolrecall/daemon-*.lck files
    socket_path  the resolved default UDS socket path
    socket       whether that socket exists
    shim_state   'active' | 'inactive' (best-effort via `shim --status --all`)
    hit_rate     cache hit-rate (from `stats`), or None if stats unavailable

Command contract (`toolrecall healthcheck`):
    - prints a single human-readable status line on stdout
    - exits 0 when the daemon is healthy
    - exits 1 and prints a `note:` line + warning when something is abnormal
      (daemon down, stale pid, leaking lock files, missing socket)

Everything is configurable via the standard TOOLRECALL_* env vars and the
`TOOLRECALL_BIN` override (cron doesn't inherit ~/.local/bin PATH).
"""

from __future__ import annotations

import os
import subprocess
import sys
from typing import TypedDict

# ── Defaults (overridable, matching the rest of the codebase) ──────────────
DEFAULT_CACHE_DIR = os.path.expanduser("~/.toolrecall")
EXIT_OK = 0
EXIT_WARN = 1
EXIT_UNKNOWN = 2


class HealthInfo(TypedDict):
    procs: int
    pid_file: str
    pid_alive: bool
    lock_files: int
    socket_path: str
    socket: bool
    shim_state: str
    hit_rate: float | None


# Honor the same env override the cron wrapper uses.
TOOLRECALL = os.environ.get("TOOLRECALL_BIN", "toolrecall")


def _default_socket_path() -> str:
    """Resolve the default UDS socket from the real transport helper."""
    try:
        from toolrecall.transport import _default_socket_path as _tsp

        return _tsp()
    except Exception:
        # Fall back to the documented default if transport is unavailable.
        return os.path.join(DEFAULT_CACHE_DIR, "toolrecall.sock")


def _pid_file() -> str:
    """Path of the daemon pid file (same as daemon.py PID_FILE)."""
    try:
        from toolrecall.daemon import PID_FILE

        return PID_FILE
    except Exception:
        return os.path.join(DEFAULT_CACHE_DIR, "daemon.pid")


def _lock_glob_dir() -> str:
    return os.path.dirname(_pid_file())


def count_daemon_procs() -> int:
    """Count live `toolrecall daemon` processes (-1 if pgrep unavailable).

    Uses the `[t]oolrecall` trailing-anchor trick so the pattern cannot match
    this checker's own command line (which contains the string literally).
    """
    try:
        r = subprocess.run(
            ["pgrep", "-af", r"[t]oolrecall daemon"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return -1
    return len([line for line in r.stdout.strip().split("\n") if line.strip()])


def gather() -> HealthInfo:
    """Collect every health signal into one dict. No side effects."""
    pid_file = _pid_file()
    lock_dir = _lock_glob_dir()

    info: HealthInfo = {
        "procs": count_daemon_procs(),
        "pid_file": "",
        "pid_alive": False,
        "lock_files": 0,
        "socket_path": _default_socket_path(),
        "socket": False,
        "shim_state": "inactive",
        "hit_rate": None,
    }

    if os.path.isfile(pid_file):
        try:
            with open(pid_file) as f:
                info["pid_file"] = f.read().strip()
        except OSError:
            info["pid_file"] = "?"

    pid = info["pid_file"]
    if pid.isdigit():
        try:
            info["pid_alive"] = os.path.exists(f"/proc/{pid}")
        except Exception:
            info["pid_alive"] = False
    elif pid:
        info["pid_alive"] = False

    if os.path.isdir(lock_dir):
        try:
            info["lock_files"] = len(
                [e for e in os.listdir(lock_dir) if e.startswith("daemon-") and e.endswith(".lck")]
            )
        except OSError:
            pass

    sock = info["socket_path"]
    if os.path.exists(sock) or os.path.islink(sock):
        try:
            info["socket"] = os.stat(sock).st_mode != 0
        except OSError:
            info["socket"] = False

    # Best-effort shim state (probe via the CLI, cheap + bounded).
    try:
        r = subprocess.run(
            [TOOLRECALL, "shim", "--status", "--all"],
            capture_output=True,
            text=True,
            timeout=20,
        )
        if "probe: pass" in r.stdout:
            info["shim_state"] = "active"
    except (subprocess.SubprocessError, FileNotFoundError):
        pass

    # Best-effort hit rate from stats (bounded; direct mode trace is fine).
    try:
        r = subprocess.run(
            [TOOLRECALL, "stats"],
            capture_output=True,
            text=True,
            timeout=25,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )
        if r.returncode == 0:
            import json as _json

            data = _json.loads(r.stdout)
            cats = {
                k: v
                for k, v in data.items()
                if isinstance(v, dict) and "hits" in v and "misses" in v
            }
            h = sum(c.get("hits", 0) for c in cats.values())
            m = sum(c.get("misses", 0) for c in cats.values())
            if (h + m) > 0:
                info["hit_rate"] = round(h / (h + m), 3)
    except Exception:
        info["hit_rate"] = None

    return info


def warnings(info: HealthInfo) -> list[str]:
    """Turn gathered signals into a list of human-readable warnings."""
    notes = []
    procs = info["procs"]
    if procs < 0:
        notes.append("daemon procs: unknown (pgrep unavailable)")
    elif procs == 0:
        notes.append("daemon NOT running (0 procs)")
    elif procs > 1:
        notes.append(f"daemon: {procs} procs — expected 1 (duplicate/leaked)")

    pid = info["pid_file"]
    if pid:
        if not info["pid_alive"]:
            notes.append(f"stale daemon.pid (pid {pid} not alive)")
    else:
        notes.append("no daemon.pid file present")

    if info["lock_files"] > 2:
        notes.append(
            f"{info['lock_files']} lock files (daemon-*.lck) — expected ≤2; "
            "stale locks never cleaned"
        )

    if not info["socket"]:
        notes.append("UDS socket not present")

    return notes


def render(info: HealthInfo) -> str:
    """One-line human summary (mirrors the cron healthcheck's format)."""
    pid = info["pid_file"] or "none"
    age = "" if info["hit_rate"] is None else f" hit_rate={info['hit_rate'] * 100:.0f}%"
    return (
        f"toolrecall healthcheck | procs={info['procs']} | pid={pid} | "
        f"locks={info['lock_files']} | sock={'yes' if info['socket'] else 'no'} "
        f"| shim={info['shim_state']}{age}"
    )


def run(as_json: bool = False) -> int:
    """Execute the healthcheck and print the report. Returns the exit code.

    ``as_json=True`` prints a single JSON object (summary over {"health": {...},
    "warnings": [...]}) instead of the human line, for cron/script consumption.
    """
    info = gather()
    notes = warnings(info)
    if as_json:
        import json as _json

        print(_json.dumps({"health": info, "warnings": notes}, default=str))
    else:
        print(render(info))
        if notes:
            print(f"  note: {'; '.join(notes)}")
    if info["procs"] == 0:
        return EXIT_WARN
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(run("--json" in sys.argv))
