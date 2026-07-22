#!/usr/bin/env python3
"""List available models via Gemini OpenAI-compatible endpoint, then test each candidate."""
import json, os, urllib.request, urllib.error

key = None
with open(os.path.expanduser("~/.hermes/.env")) as f:
    for line in f:
        line = line.strip()
        if "GOOGLE_API_KEY" in line and "=" in line and not line.startswith("#"):
            key = line.split("=", 1)[1].strip().strip('"').strip("'")
            break

if not key:
    print("No key found")
    exit(1)

# Try listing models
req = urllib.request.Request(
    "https://generativelanguage.googleapis.com/v1beta/openai/models",
    headers={"Authorization": f"Bearer {key}"},
)
try:
    resp = json.loads(urllib.request.urlopen(req, timeout=15).read())
    print("Available models:")
    for m in resp.get("data", []):
        print(f"  {m['id']}")
except Exception as e:
    print(f"List models failed: {e}")

print("\n--- Testing gemini-1.5-flash with various auth methods ---")

# Try with key as query param (v1beta)
url = f"https://generativelanguage.googleapis.com/v1beta/openai/chat/completions?key={key}"
body = json.dumps({
    "model": "models/gemini-1.5-flash",
    "messages": [{"role": "user", "content": "Reply OK"}],
    "max_tokens": 10,
}).encode()
req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
try:
    resp = json.loads(urllib.request.urlopen(req, timeout=15).read())
    print(f"✓ models/gemini-1.5-flash with query key: {resp['choices'][0]['message']['content'][:50]}")
except urllib.error.HTTPError as e:
    print(f"✗ models/gemini-1.5-flash with query key: {e.code} {e.read().decode()[:150]}")
except Exception as e:
    print(f"✗ models/gemini-1.5-flash with query key: {e}")