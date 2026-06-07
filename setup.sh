#!/usr/bin/env bash
# ToolRecall Setup -- install auto-caching for your agent
set -e

echo "=== ToolRecall Auto-Cache Setup ==="
echo ""

# 1. Install
echo "[1/4] Installing ToolRecall..."
pip install toolrecall 2>/dev/null || pip install git+https://github.com/Robin/toolrecall.git 2>/dev/null || {
    echo "Error: pip install failed"
    exit 1
}
echo "   OK"

# 2. Init script
echo "[2/4] Creating init script..."
mkdir -p ~/.toolrecall
curl -s https://raw.githubusercontent.com/Robin/toolrecall/main/toolrecall/hermes_init.py -o ~/.toolrecall/hermes_init.py
echo "   OK"

# 3. Skill
echo "[3/4] Installing agent skill..."
if command -v hermes &>/dev/null; then
    hermes skills install https://raw.githubusercontent.com/Robin/toolrecall/main/examples/hermes-skill/SKILL.md --name toolrecall 2>/dev/null || {
        mkdir -p ~/.hermes/skills/cache/toolrecall
        curl -s https://raw.githubusercontent.com/Robin/toolrecall/main/examples/hermes-skill/SKILL.md -o ~/.hermes/skills/cache/toolrecall/SKILL.md
    }
fi
echo "   OK"

# 4. Config
echo "[4/4] Registering in agent config..."
if command -v hermes &>/dev/null; then
    hermes config set agent.init_scripts '["~/.toolrecall/hermes_init.py"]' 2>/dev/null || true
fi
echo "   OK"

echo ""
echo "=== Done! Restart your agent or run /reset. ==="