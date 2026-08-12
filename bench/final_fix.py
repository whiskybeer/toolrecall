#!/usr/bin/env python3
import os

BENCH = os.path.expanduser("~/toolrecall/bench")

def patch(name, old, new):
    path = os.path.join(BENCH, name)
    data = open(path).read()
    if old not in data:
        print(f"SKIP {name}: pattern not found")
        return
    data = data.replace(old, new)
    open(path, 'w').write(data)
    print(f"OK   {name}")

# agent.py: model default
patch("agent.py", '"deepseek": "deepseek-chat"', '"deepseek": "deepseek/deepseek-v4-flash"')

# agent.py: pricing key
patch("agent.py", "    \"deepseek\": {\n        \"deepseek-chat\": {\n            \"prompt\": 0.14,\n            \"prompt_cached\": 0.0028,\n            \"completion\": 0.55,\n        },\n    },", "    \"deepseek\": {\n        \"deepseek/deepseek-v4-flash\": {\n            \"prompt\": 0.14,\n            \"prompt_cached\": 0.0028,\n            \"completion\": 0.55,\n        },\n    },")

# agent.py: fix client imports
patch("agent.py", "from toolrecall.client import cached_read, cached_write, context_set_checkpoint, context_get_dirty", "from toolrecall import cached_read, cached_write\nfrom toolrecall.context_tracker import ContextTracker\n\n    _TRACKER = ContextTracker()")
patch("agent.py", "from toolrecall.client import cached_read", "from toolrecall import cached_read")

# agent.py: fix context function calls
patch("agent.py", "context_set_checkpoint(\"turn_start\")", "_TRACKER.reset(); _TRACKER.set_checkpoint(\"turn_start\")")
patch("agent.py", "ctx = context_get_dirty()", "ctx = _TRACKER.get_dirty()")

# run_arm.py: context limit
patch("run_arm.py", "CONTEXT_LIMIT = 1_048_576   # exhaustion threshold", "CONTEXT_LIMIT = 128_000   # 128K budget")

# run_arm.py: fix context_reset
patch("run_arm.py", "from toolrecall.client import context_reset", "from toolrecall.context_tracker import ContextTracker")
patch("run_arm.py", "context_reset()", "_TRACKER = ContextTracker(); _TRACKER.reset()")

# run_arm.py: CLI choices
patch("run_arm.py", 'parser.add_argument("arm", choices=["naive", "prefix", "toolrecall"])', 'parser.add_argument("arm", choices=["naive", "prefix", "toolrecall", "hermes_compress", "both"])')

# interleave.py: ARMS
patch("interleave.py", 'ARMS = ["naive", "prefix", "toolrecall"]', 'ARMS = ["prefix", "toolrecall"]')

print("\nDone")