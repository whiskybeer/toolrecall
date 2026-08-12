# agent.py: model default
s/"deepseek": "deepseek-chat"/"deepseek": "deepseek\/deepseek-v4-flash"/
# agent.py: pricing key
s/"deepseek-chat": /"deepseek\/deepseek-v4-flash": /
# agent.py: toolrecall.client imports -> toolrecall
s/from toolrecall\.client import/from toolrecall import/
# run_arm.py: context limit
s/CONTEXT_LIMIT = 1_048_576   # exhaustion threshold/CONTEXT_LIMIT = 128_000   # 128K budget/
# interleave.py: ARMS (only the list def, not other occurrences)
/^ARMS = /s/("naive", "prefix", "toolrecall")/("prefix", "toolrecall")/
# turnlog.py: compression columns
s/context_tracker_ok     INTEGER DEFAULT 1,/context_tracker_ok     INTEGER DEFAULT 1,\n    compression_count            INTEGER DEFAULT 0,\n    compression_prompt_tokens    INTEGER DEFAULT 0,\n    compression_completion_tokens INTEGER DEFAULT 0,/
