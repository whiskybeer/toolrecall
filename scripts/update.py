#!/usr/bin/env python3
"""
ToolRecall Updater — auto-detect install method and update to latest.

Supports both install methods:
    pip install       → pip install --upgrade toolrecall
    local repo (git)  → git pull

Usage:
    python3 scripts/update.py              # interactive
    python3 scripts/update.py --force      # skip confirmations
    python3 scripts/update.py --check      # check version without updating
"""

import argparse
import os
import subprocess
import sys

REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FORCE = False


def log(msg: str, icon: str = " "):
    print(f"  {icon} {msg}")


def confirm(msg: str) -> bool:
    if FORCE:
        return True
    reply = input(f"  {msg} [y/N] ").strip().lower()
    return reply in ("y", "yes")


def get_current_version() -> str:
    """Read version from the installed package or pyproject.toml."""
    # Try installed package first
    try:
        from toolrecall import __version__
        return __version__
    except ImportError:
        pass
    # Fallback: read from pyproject.toml
    pyproject = os.path.join(REPO_DIR, "pyproject.toml")
    if os.path.isfile(pyproject):
        import tomllib
        with open(pyproject, "rb") as f:
            data = tomllib.load(f)
        return data.get("project", {}).get("version", "unknown")
    return "unknown"


def detect_install_method() -> str:
    """Detect how ToolRecall is installed.

    Returns: 'pip', 'repo', or 'unknown'
    """
    result = subprocess.run(
        [sys.executable, "-m", "pip", "show", "toolrecall"],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode == 0:
        for line in result.stdout.splitlines():
            if line.startswith("Location:"):
                loc = line.split(": ", 1)[1]
                # Check if pip-installed version points to the repo (editable install)
                if REPO_DIR in loc:
                    return "repo"
                return "pip"
    # Not pip-installed — check if running from repo
    if os.path.isdir(os.path.join(REPO_DIR, ".git")):
        return "repo"
    return "unknown"


def detect_daemon_running() -> bool:
    """Check if the daemon is running (UDS socket or PID file)."""
    import socket, json
    sock_path = os.path.expanduser("~/.toolrecall/tc.sock")
    if os.path.exists(sock_path):
        try:
            s = socket.socket(socket.AF_UNIX)
            s.settimeout(1)
            s.connect(sock_path)
            s.sendall(json.dumps({"action": "ping"}).encode())
            resp = json.loads(s.recv(4096).decode())
            s.close()
            return resp.get("status") == "pong"
        except (OSError, json.JSONDecodeError):
            pass
    return False


def step_announce(current: str, method: str):
    print()
    print("╔══════════════════════════════════════════════╗")
    print("║        ToolRecall Updater                   ║")
    print("╚══════════════════════════════════════════════╝")
    print(f"  Current version: {current}")
    print(f"  Install method:  {method}")
    print()


# ── Update methods ────────────────────────────────────────────

def update_via_pip(current: str) -> bool:
    """Update via pip install --upgrade."""
    print("[1/3] Upgrading pip package...")
    result = subprocess.run(
        [sys.executable, "-m", "pip", "install", "--upgrade", "toolrecall"],
        capture_output=True, text=True, timeout=120,
    )
    if result.returncode != 0:
        log(f"pip upgrade failed:\n{result.stderr}", "✗")
        return False
    log("Package upgraded", "✓")

    # Verify
    result = subprocess.run(
        [sys.executable, "-m", "pip", "show", "toolrecall"],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode == 0:
        for line in result.stdout.splitlines():
            if line.startswith("Version:"):
                log(f"New version: {line.split(': ', 1)[1]}", "✓")
    return True


def update_via_git(current: str) -> bool:
    """Update via git pull on the repo."""
    print("[1/3] Updating from git...")

    # Check for uncommitted changes
    result = subprocess.run(
        ["git", "status", "--porcelain"],
        capture_output=True, text=True, timeout=10, cwd=REPO_DIR,
    )
    if result.stdout.strip():
        log("Uncommitted changes detected", "⚠")
        log("Commit or stash before updating", "→")
        if confirm("Stash changes and continue?"):
            subprocess.run(
                ["git", "stash", "-u"],
                capture_output=True, timeout=10, cwd=REPO_DIR,
            )
            log("Changes stashed", "✓")
        else:
            log("Update cancelled", "−")
            return False

    # Determine upstream branch
    result = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "@{upstream}"],
        capture_output=True, text=True, timeout=10, cwd=REPO_DIR,
    )
    if result.returncode != 0:
        log("No upstream branch configured", "⚠")
        if confirm("Set upstream to origin/main?"):
            subprocess.run(
                ["git", "branch", "--set-upstream-to", "origin/main"],
                capture_output=True, timeout=10, cwd=REPO_DIR,
            )
            log("Upstream set to origin/main", "✓")
        else:
            log("Update cancelled", "−")
            return False

    # Pull
    result = subprocess.run(
        ["git", "pull", "--ff-only"],
        capture_output=True, text=True, timeout=60, cwd=REPO_DIR,
    )
    if result.returncode != 0:
        log(f"git pull failed: {result.stderr.strip()}", "✗")
        return False
    log(f"Git output: {result.stdout.strip()}", "✓")
    return True


def restart_daemon():
    """Restart the daemon if it was running before the update."""
    print("[2/3] Restarting daemon...")
    was_running = detect_daemon_running()
    if not was_running:
        log("Daemon was not running — skipping restart", "−")
        return

    # Stop
    subprocess.run(
        ["toolrecall", "daemon", "--stop"],
        capture_output=True, timeout=10,
    )
    # Start
    subprocess.run(
        ["toolrecall", "daemon", "&"],
        capture_output=True, timeout=10, shell=True,
    )
    log("Daemon restarted", "✓")


def verify():
    """Verify the update by checking the installed version is importable."""
    print("[3/3] Verification...")
    try:
        # Clear any cached import
        for mod in list(sys.modules.keys()):
            if mod.startswith("toolrecall"):
                del sys.modules[mod]
        from toolrecall import cached_read, __version__
        log(f"Import OK — version {__version__}", "✓")
        log("ToolRecall is ready", "✓")
        return True
    except ImportError as e:
        log(f"Import failed after update: {e}", "✗")
        return False


# ── Main ──────────────────────────────────────────────────────

def main():
    global FORCE
    parser = argparse.ArgumentParser(description="ToolRecall Updater")
    parser.add_argument("--force", action="store_true", help="Skip confirmations")
    parser.add_argument("--check", action="store_true", help="Check version without updating")
    args = parser.parse_args()
    FORCE = args.force

    current = get_current_version()
    method = detect_install_method()

    step_announce(current, method)

    if args.check:
        log(f"ToolRecall {current} ({method}) — no update performed", "✓")
        return

    if method == "unknown":
        log("Could not detect install method", "✗")
        log("Update manually: pip install --upgrade toolrecall", "→")
        sys.exit(1)

    if not confirm(f"Update ToolRecall ({current})?"):
        log("Update cancelled", "−")
        return

    if method == "pip":
        ok = update_via_pip(current)
    else:
        ok = update_via_git(current)

    if not ok:
        log("Update failed", "✗")
        sys.exit(1)

    restart_daemon()
    verify()

    print()
    log("Update complete", "✓")
    print()
    log("If this was a major version update (>0.x.0),", "ℹ")
    log("run python3 scripts/uninstall.py --force --dry-run")
    log("and reinstall fresh to avoid stale cache issues.")


if __name__ == "__main__":
    main()
