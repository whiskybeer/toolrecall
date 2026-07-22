#!/usr/bin/env python3
"""Check OpenRouter model info and provider options for DeepSeek V4 Flash."""
import json, os, urllib.request, urllib.error

# Get OpenRouter key
key = None
with open(os.path.expanduser("~/.hermes/.env")) as f:
    for line in f:
        line = line.strip()
        if "OPENROUTER_API_KEY" in line and "=" in line and not line.startswith("#"):
            key = line.split("=", 1)[1].strip().strip('"').strip("'")

# Try to get model info from OpenRouter
url = "https://openrouter.ai/api/v1/models"
req = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}"})
try:
    resp = json.loads(urllib.request.urlopen(req, timeout=15).read())
    for m in resp.get("data", []):
        if "deepseek" in m.get("id", "").lower() and "flash" in m.get("id", "").lower():
            print(f"Model: {m['id']}")
            print(f"  Context: {m.get('context_length', '?')}")
            print(f"  Pricing: {json.dumps(m.get('pricing', {}))}")
            print(f"  Provider routes: {json.dumps(m.get('endpoints', m.get('provider', '?')), indent=2)}")
            print()
except urllib.error.HTTPError as e:
    print(f"HTTP {e.code}: {e.read().decode()[:300]}")
except Exception as e:
    print(f"Error: {e}")