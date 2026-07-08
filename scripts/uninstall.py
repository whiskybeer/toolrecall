#!/usr/bin/env python3
"""ToolRecall Uninstaller — removes ONLY what the setup created.

Usage:
    python3 scripts/uninstall.py              # Interactive (confirms each step)
    python3 scripts/uninstall.py --force      # Skip confirmations
    python3 scripts/uninstall.py --help       # This message

Supports both:
    - pipx-installed (pipx install toolrecall)
    - pip-installed (pip install toolrecall)
    - local-repo   (import from ~/toolrecall/ via PYTHONPATH)
"""

import argparse
import os
import re
import shutil
import subprocess
import sys

HOME = os.path.expanduser("~")
TR_DIR = os.path.join(HOME, ".toolrecall")
REPO_DIR = os.path.join(HOME, "toolrecall")
HERMES_CONFIG = os.path.join(HOME, ".hermes", "config.yaml")
SANDBOX_CONFIG = os.path.join(HOME, ".hermes", "sandbox.yaml")
SYSTEMD_SERVICE = os.path.join(HOME, ".config", "systemd", "user", "toolrecall-daemon.service")
CRON_REMINDER = os.path.join(HOME, ".hermes", "scripts", "uninstall-toolrecall-reminder.md")
SKILL_DIRS = [
    os.path.join(HOME, ".hermes", "skills", "cache", "toolrecall"),
    os.path.join(HOME, ".hermes", "skills", "software-development", "tool-recall"),
]

REMOVED = 0
SKIPPED = 0
FORCE = False


def log(msg: str, icon: str = " "):
    print(f"  {icon} {msg}")


def confirm(msg: str) -> bool:
    if FORCE:
        return True
    reply = input(f"  {msg} [y/N] ").strip().lower()
    return reply in ("y", "yes")


def announce():
    print()
    print("╔══════════════════════════════════════════════╗")
    print("║        ToolRecall Uninstaller                ║")
    print("╚══════════════════════════════════════════════╝")
    print()


def summary():
    print()
    print("────────────────────────────────────────────")
    print(f"  Removed: {REMOVED}   Skipped: {SKIPPED}")
    print("────────────────────────────────────────────")
    print()
    if os.path.isdir(REPO_DIR):
        print("  To also remove the ToolRecall repo:")
        print(f"    rm -rf {REPO_DIR}")
    print()
    print("  Done. Start a fresh session (or /reset) to")
    print("  confirm toolrecall tools are gone.")
    print()


# ── 1. Stop running processes ──────────────────────────────────

def step_stop_daemon():
    global REMOVED
    print("\n[1/9] Stopping running processes...")
    stopped = False

    # Try CLI stop via toolrecall command
    for cmd in [
        ["toolrecall", "daemon", "--stop"],
        [sys.executable, "-m", "toolrecall", "daemon", "--stop"],
    ]:
        try:
            subprocess.run(cmd, capture_output=True, timeout=10)
            stopped = True
            break
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue

    # Kill by PID file (try both old and new path)
    for pid_path in [os.path.join(TR_DIR, "tc.pid"), os.path.join(TR_DIR, "daemon.pid")]:
        if os.path.isfile(pid_path):
            try:
                pid = int(open(pid_path).read().strip())
                os.kill(pid, 15)
                stopped = True
            except (ValueError, OSError, ProcessLookupError):
                pass

    # Kill any remaining daemon processes
    try:
        subprocess.run(
            ["pkill", "-f", "toolrecall daemon"],
            capture_output=True, timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    if stopped:
        log("Daemon stopped", "✓")
    else:
        log("No daemon to stop", "−")


# ── 2. Systemd user service ────────────────────────────────────

def step_systemd():
    global REMOVED, SKIPPED
    print("\n[2/9] Systemd user service...")
    if not os.path.isfile(SYSTEMD_SERVICE):
        log("No systemd service found", "−")
        return

    if confirm("Remove systemd service?"):
        # systemctl might not exist (container, minimal install)
        for cmd in [
            ["systemctl", "--user", "stop", "toolrecall-daemon"],
            ["systemctl", "--user", "disable", "toolrecall-daemon"],
            ["systemctl", "--user", "daemon-reload"],
        ]:
            try:
                subprocess.run(cmd, capture_output=True, timeout=10)
            except FileNotFoundError:
                pass  # systemctl not available, still remove the file
        os.remove(SYSTEMD_SERVICE)
        log("Removed systemd service", "✓")
        REMOVED += 1
    else:
        log("Skipped", "−")
        SKIPPED += 1


# ── 3. Data directory ──────────────────────────────────────────

def step_data_dir():
    global REMOVED, SKIPPED
    print(f"\n[3/9] Data directory ({TR_DIR})...")
    if not os.path.isdir(TR_DIR):
        log("No data directory found", "−")
        return

    if confirm(f"Remove {TR_DIR} (cache DB, config, logs)?"):
        shutil.rmtree(TR_DIR)
        log(f"Removed {TR_DIR}", "✓")
        REMOVED += 1
    else:
        log("Skipped", "−")
        SKIPPED += 1


# ── 4. Hermes config edits ─────────────────────────────────────

def edit_config_file(path: str, label: str):
    """Remove toolrecall references from a YAML config file."""
    if not os.path.isfile(path):
        return False

    with open(path) as f:
        content = f.read()

    original = content
    changed = False

    # Remove mcp_servers.toolrecall block (multi-line)
    content = re.sub(
        r'^  toolrecall:\n(?:    .+\n)+',
        '',
        content,
        flags=re.MULTILINE,
    )

    if content != original:
        with open(path, "w") as f:
            f.write(content)
        return True
    return False


def step_hermes_config():
    global REMOVED, SKIPPED
    print(f"\n[4/9] Hermes config ({HERMES_CONFIG})...")

    if not os.path.isfile(HERMES_CONFIG):
        log("Config file not found", "−")
        return

    has_refs = False
    with open(HERMES_CONFIG) as f:
        raw = f.read()
    has_refs = "toolrecall" in raw

    if not has_refs:
        log("No toolrecall references in config", "−")
        return

    if confirm("Remove toolrecall entries from config.yaml (mcp_servers)?"):
        if edit_config_file(HERMES_CONFIG, "config.yaml"):
            log("Cleaned config.yaml", "✓")
            REMOVED += 1
        else:
            log("No changes needed", "−")
    else:
        log("Skipped", "−")
        SKIPPED += 1


# ── 5. Sandbox config ──────────────────────────────────────────

def step_sandbox():
    global REMOVED, SKIPPED
    print("\n[5/9] Sandbox config...")

    if not os.path.isfile(SANDBOX_CONFIG):
        log("No sandbox.yaml found", "−")
        return

    with open(SANDBOX_CONFIG) as f:
        content = f.read()

    if "toolrecall" not in content:
        log("No toolrecall references in sandbox.yaml", "−")
        return

    if confirm("Remove toolrecall path references from sandbox.yaml?"):
        content = content.replace(REPO_DIR, "")
        # Clean up empty list items left behind
        content = re.sub(r'\n\s*-\s*""\s*', '', content)
        with open(SANDBOX_CONFIG, "w") as f:
            f.write(content)
        log("Cleaned sandbox.yaml", "✓")
        REMOVED += 1
    else:
        log("Skipped", "−")
        SKIPPED += 1


# ── 6. Cron jobs ──────────────────────────────────────────────

def step_cron():
    global REMOVED, SKIPPED
    print("\n[6/9] Hermes cron jobs...")

    print("  ! Cron jobs referencing toolrecall still exist:")
    print("    - toolrecall-watchdog (runs every 10m)")
    print("    - memory-db-sync     (runs every 60m — syncs memory to FTS5)")
    print()
    print("  Remove them via the agent:")
    print('    "remove the toolrecall-watchdog and memory-db-sync cron jobs"')
    print()

    if confirm("Write reminder file?"):
        os.makedirs(os.path.dirname(CRON_REMINDER), exist_ok=True)
        with open(CRON_REMINDER, "w") as f:
            f.write("# Cron jobs to remove after uninstall:\n")
            f.write("# toolrecall-watchdog — every 10m\n")
            f.write("# memory-db-sync     — every 60m\n")
        log(f"Reminder at {CRON_REMINDER}", "✓")
        REMOVED += 1
    else:
        log("Skipped", "−")
        SKIPPED += 1


# ── 7. Hermes skills ───────────────────────────────────────────

def step_skills():
    global REMOVED, SKIPPED
    print("\n[7/9] Hermes skills...")

    for skill_dir in SKILL_DIRS:
        if not os.path.isdir(skill_dir):
            continue

        name = os.path.basename(skill_dir)
        if confirm(f"Remove skill '{name}'?"):
            shutil.rmtree(skill_dir)
            log(f"Removed {name}", "✓")
            REMOVED += 1
        else:
            log(f"Skipped {name}", "−")
            SKIPPED += 1


# ── 8. VS Code Extension ──────────────────────────────────────

def step_vscode():
    global REMOVED, SKIPPED
    print("\n[8/9] VS Code Extension...")

    # Check for installed extension
    result = subprocess.run(
        ["code", "--list-extensions"],
        capture_output=True, text=True, timeout=10,
    )
    if result.returncode != 0 or "toolrecall" not in result.stdout:
        log("ToolRecall VS Code extension not installed", "−")
        return

    if confirm("Uninstall ToolRecall VS Code extension?"):
        subprocess.run(
            ["code", "--uninstall-extension", "whiskybeer.toolrecall-cache"],
            capture_output=True, timeout=10,
        )
        log("Uninstalled VS Code extension", "✓")
        REMOVED += 1
    else:
        log("Skipped", "−")
        SKIPPED += 1


# ── 9. Pip/pipx package ────────────────────────────────────────

def step_pip():
    global REMOVED, SKIPPED
    print("\n[9/9] Pip/pipx package...")

    # Check pipx first (preferred install method)
    pipx_found = False
    try:
        r = subprocess.run(
            ["pipx", "list"], capture_output=True, text=True, timeout=15,
        )
        pipx_found = r.returncode == 0 and "toolrecall" in r.stdout
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    if pipx_found:
        if confirm("Uninstall pipx package 'toolrecall'?"):
            try:
                subprocess.run(
                    ["pipx", "uninstall", "toolrecall"],
                    capture_output=True, timeout=30,
                )
                log("Uninstalled pipx package", "✓")
                REMOVED += 1
            except (FileNotFoundError, subprocess.TimeoutExpired):
                log("pipx uninstall failed", "⚠")
                SKIPPED += 1
        else:
            log("Skipped", "−")
            SKIPPED += 1
        return

    # Check pip
    result = subprocess.run(
        [sys.executable, "-m", "pip", "show", "toolrecall"],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        log("Not pip-installed (local repo only)", "−")
        return

    if confirm("Uninstall pip package 'toolrecall'?"):
        subprocess.run(
            [sys.executable, "-m", "pip", "uninstall", "-y", "toolrecall"],
            capture_output=True, timeout=30,
        )
        log("Uninstalled pip package", "✓")
        REMOVED += 1
    else:
        log("Skipped", "−")
        SKIPPED += 1


# ── Main ───────────────────────────────────────────────────────

def main():
    global FORCE
    parser = argparse.ArgumentParser(description="ToolRecall Uninstaller")
    parser.add_argument(
        "--force", action="store_true",
        help="Skip all confirmations (non-interactive)",
    )
    args = parser.parse_args()
    FORCE = args.force

    announce()

    step_stop_daemon()
    step_systemd()
    step_data_dir()
    step_hermes_config()
    step_sandbox()
    step_cron()
    step_skills()
    step_vscode()
    step_pip()

    summary()


if __name__ == "__main__":
    main()
