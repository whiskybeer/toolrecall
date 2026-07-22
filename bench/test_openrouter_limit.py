#!/usr/bin/env python3
"""Test: what does OpenRouter actually limit at?"""
import json, os, urllib.request, urllib.error

# Get OpenRouter key
key = None
with open(os.path.expanduser("~/.hermes/.env")) as f:
    for line in f:
        line = line.strip()
        if "OPENROUTER_API_KEY" in line and "=" in line and not line.startswith("#"):
            key = line.split("=", 1)[1].strip().strip('"').strip("'")

# Probe: what's the real endpoint limit?
big_msg = {"role": "user", "content": "hello " * 30000}  # ~120K tokens
body = json.dumps({
    "model": "deepseek/deepseek-v4-flash",
    "messages": [big_msg],
    "max_tokens": 10,
}).encode()

req = urllib.request.Request(
    "https://openrouter.ai/api/v1/chat/completions",
    data=body,
    headers={
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    },
)
try:
    resp = json.loads(urllib.request.urlopen(req, timeout=60).read())
    usage = resp.get("usage", {})
    print(f"OK - prompt_tokens: {usage.get('prompt_tokens', '?')}")
except urllib.error.HTTPError as e:
    print(f"HTTP {e.code}:")
    print(e.read().decode()[:600])
except Exception as e:
    print(f"Error: {e}")