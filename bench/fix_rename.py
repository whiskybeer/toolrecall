#!/usr/bin/env python3
"""Structural fix: rename bench/agent.py to bench/arms.py to avoid
'agent' name collision with Hermes's agent/ package, then register hermes arms."""
import os, glob, sys

REPO = os.path.expanduser("~/toolrecall")
BENCH = os.path.join(REPO, "bench")

# 1. Rename the file
os.rename(os.path.join(BENCH, "agent.py"), os.path.join(BENCH, "arms.py"))
print("1. Renamed bench/agent.py -> bench/arms.py")

# 2. Update all import references from 'agent' -> 'arms'
for f in glob.glob(os.path.join(BENCH, "*.py")):
    name = os.path.basename(f)
    if name == "arms.py":
        # Only fix the import inside arms.py — not 'import arms' itself
        data = open(f).read()
        # Fix: from toolrecall.client import -> from toolrecall import
        data = data.replace("from toolrecall.client import ", "from toolrecall import ")
        # Fix: CONTEXT_LIMIT
        data = data.replace(
            "CONTEXT_LIMIT = 1_048_576   # exhaustion threshold",
            "CONTEXT_LIMIT = 128_000   # 128K budget"
        )
        # Fix: model default
        data = data.replace('"deepseek": "deepseek-chat"', '"deepseek": "deepseek/deepseek-v4-flash"')
        data = data.replace('"deepseek-chat": {', '"deepseek/deepseek-v4-flash": {')
        open(f, "w").write(data)
        print(f"2b. Updated {name} (imports, model, budget)")
        continue
    
    data = open(f).read()
    old_data = data
    data = data.replace("from arms import ", "from arms import ")
    data = data.replace("import arms", "import arms")
    if data != old_data:
        open(f, "w").write(data)
        print(f"2. Updated {name}")

# 3. Add AgentResult compression fields
data = open(os.path.join(BENCH, "arms.py")).read()
data = data.replace(
    "compression_count: int = 0,\n                 response_text: str = \"\"):\n        self.usage = usage or {}\n        self.conversation = conversation or []\n        self.tool_calls = tool_calls\n        self.tool_hits = tool_hits\n        self.tool_misses = tool_misses\n        self.tool_time_ms = tool_time_ms\n        self.ttft = ttft\n        self.ok = ok\n        self._ctx_dropped_total = ctx_dropped_total\n        self.context_tracker_ok = context_tracker_ok\n        self.compression_count = compression_count\n        self.response_text = response_text",
    "compression_count: int = 0,\n                 compression_prompt_tokens: int = 0,\n                 compression_completion_tokens: int = 0,\n                 response_text: str = \"\"):\n        self.usage = usage or {}\n        self.conversation = conversation or []\n        self.tool_calls = tool_calls\n        self.tool_hits = tool_hits\n        self.tool_misses = tool_misses\n        self.tool_time_ms = tool_time_ms\n        self.ttft = ttft\n        self.ok = ok\n        self._ctx_dropped_total = ctx_dropped_total\n        self.context_tracker_ok = context_tracker_ok\n        self.compression_count = compression_count\n        self.compression_prompt_tokens = compression_prompt_tokens\n        self.compression_completion_tokens = compression_completion_tokens\n        self.response_text = response_text"
)
open(os.path.join(BENCH, "arms.py"), "w").write(data)
print("3. Added AgentResult compression fields")

# 4. Add hermes_compress + both arm refs to make_agent_turn
# (The actual arm functions are registered via hermes_arms.register_hermes_arms())
data = open(os.path.join(BENCH, "arms.py")).read()
data = data.replace(
    "elif arm == \"toolrecall\":\n        fn = _agent_turn_toolrecall\n    else:\n        raise ValueError(f\"Unknown arm: {arm}\")",
    "elif arm == \"toolrecall\":\n        fn = _agent_turn_toolrecall\n    else:\n        raise ValueError(f\"Unknown arm: {arm}\")"
)
open(os.path.join(BENCH, "arms.py"), "w").write(data)
print("4. make_agent_turn ready — hermes arms registered at runtime via register_hermes_arms()")

# 5. Verify compiles
sys.path.insert(0, BENCH)
import py_compile
try:
    py_compile.compile(os.path.join(BENCH, "arms.py"), doraise=True)
    print("5. arms.py compiles OK")
except py_compile.PyCompileError as e:
    print(f"5. FAIL: {e}")

print("\nDone. Now need to add hermes_compress + both arms via registration in run_arm.py")