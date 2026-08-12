#!/usr/bin/env python3
"""Minimal fixes to run the 4-arm benchmark. Applies patches to git-restored files."""
import os

REPO = os.path.expanduser("~/toolrecall")

def p(fname, old, new):
    path = os.path.join(REPO, fname)
    data = open(path).read()
    if old not in data:
        # Find similar text
        for line in data.split('\n'):
            if old[:20] in line:
                print(f"  NEAR MISS in {fname}: {line.strip()[:80]}")
        return False
    data = data.replace(old, new)
    open(path, 'w').write(data)
    return True

# 1. agent.py: model default
p("bench/agent.py",
    '"deepseek": "deepseek-chat"',
    '"deepseek": "deepseek/deepseek-v4-flash"')

# 2. agent.py: pricing
p("bench/agent.py",
    '"deepseek-chat": {',
    '"deepseek/deepseek-v4-flash": {')

# 3. agent.py: fix client imports (2 occurrences)
p("bench/agent.py",
    "from toolrecall.client import cached_read, cached_write, context_set_checkpoint, context_get_dirty",
    "from toolrecall import cached_read, cached_write\nfrom toolrecall.context_tracker import ContextTracker\n    _TRACKER = ContextTracker()")

# 4. agent.py: fix context_set_checkpoint call
p("bench/agent.py",
    "context_set_checkpoint(\"turn_start\")",
    "_TRACKER.reset(); _TRACKER.set_checkpoint(\"turn_start\")")

# 5. agent.py: fix context_get_dirty call
p("bench/agent.py",
    "ctx = context_get_dirty()",
    "ctx = _TRACKER.get_dirty()")

# 6. agent.py: fix the naive arm import
p("bench/agent.py",
    "from toolrecall.client import cached_read",
    "from toolrecall import cached_read")

# 7. run_arm.py: context limit
p("bench/run_arm.py",
    "CONTEXT_LIMIT = 1_048_576   # exhaustion threshold",
    "CONTEXT_LIMIT = 128_000   # 128K budget")

# 8. run_arm.py: fix client import
p("bench/run_arm.py",
    "from toolrecall.client import cached_read",
    "from toolrecall import cached_read")

# 9. run_arm.py: fix context_reset
p("bench/run_arm.py",
    "from toolrecall.client import context_reset\n        context_reset()",
    "from toolrecall.context_tracker import ContextTracker\n        _TRACKER = ContextTracker()\n        _TRACKER.reset()")

# 10. interleave.py: ARMS
p("bench/interleave.py",
    'ARMS = ["naive", "prefix", "toolrecall"]',
    'ARMS = ["prefix", "toolrecall"]')

# 11. turnlog.py: add compression columns
p("bench/turnlog.py",
    "context_tracker_ok     INTEGER DEFAULT 1,\n    PRIMARY KEY (run_id, turn_index)",
    "context_tracker_ok     INTEGER DEFAULT 1,\n    compression_count            INTEGER DEFAULT 0,\n    compression_prompt_tokens    INTEGER DEFAULT 0,\n    compression_completion_tokens INTEGER DEFAULT 0,\n    PRIMARY KEY (run_id, turn_index)")

print("All patches applied.")