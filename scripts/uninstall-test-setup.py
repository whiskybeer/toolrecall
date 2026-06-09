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

os.makedirs("/root/.toolrecall/logs")
for f in ["cache.db", "knowledge.db", "daemon.log", "watchdog-status.json", "nginx-toolrecall.conf"]:
    open(f"/root/.toolrecall/{f}", "w").close()
with open("/root/.toolrecall/config.toml", "w") as f:
    f.write('[paths]\ncache_db = "/root/.toolrecall/cache.db"\n')
with open("/root/.toolrecall/hermes_init.py", "w") as f:
    f.write("print('hermes_init loaded')\n")

os.makedirs("/root/.config/systemd/user")
with open("/root/.config/systemd/user/toolrecall-daemon.service", "w") as f:
    f.write("[Unit]\nDescription=ToolRecall Daemon\n[Service]\nExecStart=/usr/bin/python3 -m toolrecall daemon --foreground\n")

os.makedirs("/root/.hermes")
with open("/root/.hermes/config.yaml", "w") as f:
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

with open("/root/.hermes/sandbox.yaml", "w") as f:
    f.write('sandbox:\n  allowed_paths:\n  - "/home/hermes/toolrecall"\n')

os.makedirs("/root/.hermes/skills/cache/toolrecall")
with open("/root/.hermes/skills/cache/toolrecall/SKILL.md", "w") as f:
    f.write("# toolrecall skill\n")

os.makedirs("/root/.hermes/skills/software-development/tool-recall")
with open("/root/.hermes/skills/software-development/tool-recall/SKILL.md", "w") as f:
    f.write("# tool-recall skill\n")

print("Test environment setup complete")
