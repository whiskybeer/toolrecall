"""Apply all import fixes for the toolrecall API changes and context tracker refactoring."""

import os

REPO = os.path.expanduser("~/toolrecall")


def patch_file(fname, old, new):
    path = os.path.join(REPO, fname)
    data = open(path).read()
    if old in data:
        data = data.replace(old, new)
        open(path, "w").write(data)
        return True
    return False


# 1. run_arm.py: CONTEXT_LIMIT to 128K + fix client imports + named DBs + CLI arms
patch_file(
    "bench/run_arm.py",
    "CONTEXT_LIMIT = 1_048_576   # exhaustion threshold",
    "CONTEXT_LIMIT = 128_000   # 128K budget — compressor fires at ~64K",
)

patch_file(
    "bench/run_arm.py",
    'parser.add_argument("arm", choices=["naive", "prefix", "toolrecall"])',
    'parser.add_argument("arm", choices=["naive", "prefix", "toolrecall", "hermes_compress", "both"])',
)

# 2. arms.py: model default
patch_file(
    "bench/agent.py", '"deepseek": "deepseek-chat"', '"deepseek": "deepseek/deepseek-v4-flash"'
)

patch_file("bench/agent.py", '"deepseek-chat": {', '"deepseek/deepseek-v4-flash": {')

# 3. Fix client imports: from toolrecall.client import -> from toolrecall import
for fname in ["bench/agent.py", "bench/run_arm.py"]:
    data = open(os.path.join(REPO, fname)).read()
    data = data.replace("from toolrecall.client import", "from toolrecall import")
    open(os.path.join(REPO, fname), "w").write(data)

# 4. Replace context_set_checkpoint/context_get_dirty/context_reset with ContextTracker class
# The toolrecall arm in agent.py
old_tr_import = """    from toolrecall import cached_read, cached_write"""

new_tr_import = """    from toolrecall import cached_read, cached_write
    from toolrecall.context_tracker import ContextTracker
    _TRACKER = ContextTracker()"""

patch_file("bench/agent.py", old_tr_import, new_tr_import)

old_tr_checkpoint = """        try:
            context_set_checkpoint(\"turn_start\")"""
new_tr_checkpoint = """        try:
            _TRACKER.reset()
            _TRACKER.set_checkpoint(\"turn_start\")"""
patch_file("bench/agent.py", old_tr_checkpoint, new_tr_checkpoint)

patch_file("bench/agent.py", "ctx = context_get_dirty()", "ctx = _TRACKER.get_dirty()")

# 5. run_arm.py: context_reset
patch_file(
    "bench/run_arm.py",
    "from toolrecall import context_reset\n        context_reset()",
    "from toolrecall.context_tracker import ContextTracker\n        _TRACKER = ContextTracker()\n        _TRACKER.reset()",
)

# 6. turnlog.py: add compression columns
schema_add = """    compression_count            INTEGER DEFAULT 0,
    compression_prompt_tokens    INTEGER DEFAULT 0,
    compression_completion_tokens INTEGER DEFAULT 0,
    PRIMARY KEY (run_id, turn_index)"""
old_schema = "    PRIMARY KEY (run_id, turn_index)"
data = open(os.path.join(REPO, "bench/turnlog.py")).read()
data = data.replace(old_schema, schema_add)
open(os.path.join(REPO, "bench/turnlog.py"), "w").write(data)

# 7. interleave.py: ARMS list
patch_file(
    "bench/interleave.py",
    'ARMS = ["naive", "prefix", "toolrecall"]',
    'ARMS = ["prefix", "toolrecall"]',
)

print("All patches applied.")
print()
print("Files changed: bench/agent.py, bench/run_arm.py, bench/interleave.py, bench/turnlog.py")
