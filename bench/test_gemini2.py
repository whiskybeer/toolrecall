#!/usr/bin/env python3
"""Test Gemini API via OpenAI-compatible endpoint with Bearer auth."""
import json, os, urllib.request, urllib.error

key = None
with open(os.path.expanduser("~/.hermes/.env")) as f:
    for line in f:
        line = line.strip()
        if "GOOGLE_API_KEY" in line and "=" in line and not line.startswith("#"):
            key = line.split("=", 1)[1].strip().strip('"').strip("'")
            break

models_to_test = [
    "gemini-2.5-flash",       # latest flash, 1M ctx
    "gemini-flash-latest",    # alias
    "gemini-2.0-flash",       # wide support, 1M ctx
    "gemini-1.5-flash",       # old but documented
]

for model in models_to_test:
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": "Reply with just OK"}],
        "max_tokens": 10,
        "temperature": 0.0,
    }).encode()

    url = "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
    req = urllib.request.Request(url, data=body, headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {key}",
    })
    try:
        resp = json.loads(urllib.request.urlopen(req, timeout=15).read())
        usage = resp.get("usage", {})
        print(f"✓ {model}: {resp['choices'][0]['message']['content'][:30]} | "
              f"in={usage.get('prompt_tokens',0)} out={usage.get('completion_tokens',0)}")
    except urllib.error.HTTPError as e:
        err = e.read().decode()[:150]
        print(f"✗ {model}: HTTP {e.code} - {err}")
    except Exception as e:
        print(f"✗ {model}: {e}")