#!/usr/bin/env python3
"""
ToolRecall Uninstaller Test — Hermes-specific test environment setup.

⚠️  This is a TEST FIXTURE for the Docker sandbox test ONLY.
    It creates a fake ToolRecall + Hermes install footprint for
    validation purposes. NOT a general-purpose tool.

    What it simulates:
    - ~/.toolrecall/       (cache, config, logs, init script)
    - systemd user service (systemctl)
    - ~/.hermes/config.yaml (init_scripts + mcp_servers refs)
    - ~/.hermes/sandbox.yaml (path refs)
    - ~/.hermes/skills/     (cache/toolrecall, tool-recall)

    If you're testing with another agent (Claude Code, Cursor, Cline),
    you'll need a different test fixture.
"""

import os
import tempfile

TEST_HOME = os.path.join(tempfile.gettempdir(), "toolrecall_uninstall_test")
os.environ["HOME"] = TEST_HOME

os.makedirs(os.path.join(TEST_HOME, ".toolrecall", "logs"))
for f in ["cache.db", "knowledge.db", "daemon.log", "watchdog-status.json", "nginx-toolrecall.conf"]:
    open(os.path.join(TEST_HOME, ".toolrecall", f), "w").close()
with open(os.path.join(TEST_HOME, ".toolrecall", "config.toml"), "w") as f:
    f.write(f'[paths]\ncache_db = "{TEST_HOME}/.toolrecall/cache.db"\n')
with open(os.path.join(TEST_HOME, ".toolrecall", "hermes_init.py"), "w") as f:
    f.write("print('hermes_init loaded')\n")

os.makedirs(os.path.join(TEST_HOME, ".config", "systemd", "user"))
with open(os.path.join(TEST_HOME, ".config", "systemd", "user", "toolrecall-daemon.service"), "w") as f:
    f.write("[Unit]\nDescription=ToolRecall Daemon\n[Service]\nExecStart=/usr/bin/python3 -m toolrecall daemon --foreground\n")

os.makedirs(os.path.join(TEST_HOME, ".hermes"))
with open(os.path.join(TEST_HOME, ".hermes", "config.yaml"), "w") as f:
    f.write("""agent:
  init_scripts: '["~/.toolrecall/hermes_init.py"]'
  max_turns: 100

mcp_servers:
  toolrecall:
    command: toolrecall
    args:
    - mcp
    timeout: 30

display:
  personality: ''
""")

REAL_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
with open(os.path.join(TEST_HOME, ".hermes", "sandbox.yaml"), "w") as f:
    f.write(f'sandbox:\n  allowed_paths:\n  - "{REAL_REPO}"\n')

os.makedirs(os.path.join(TEST_HOME, ".hermes", "skills", "cache", "toolrecall"))
with open(os.path.join(TEST_HOME, ".hermes", "skills", "cache", "toolrecall", "SKILL.md"), "w") as f:
    f.write("# toolrecall skill\n")

os.makedirs(os.path.join(TEST_HOME, ".hermes", "skills", "software-development", "tool-recall"))
with open(os.path.join(TEST_HOME, ".hermes", "skills", "software-development", "tool-recall", "SKILL.md"), "w") as f:
    f.write("# tool-recall skill\n")

print(f"Test environment setup complete at {TEST_HOME}")
