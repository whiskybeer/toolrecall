#!/usr/bin/env python3
"""Probe DeepSeek context limit with a large payload."""
import json, os, urllib.request, urllib.error, socket

# Get key from env or .env
api_key = os.environ.get("DEEPSEEK_API_KEY")
if not api_key:
    with open(os.path.expanduser("~/.hermes/.env")) as f:
        for line in f:
            line = line.strip()
            if line.startswith("DEEPSEEK_API_KEY") and "=" in line and not line.startswith("#"):
                api_key = line.split("=", 1)[1].strip().strip('"').strip("'")
                break

for target_chars in [50000, 100000, 200000, 400000]:
    msg = "hello world " * (target_chars // 12)  # ~0.25 tok/char
    body = json.dumps({
        "model": "deepseek-chat",
        "messages": [{"role": "user", "content": msg}],
        "max_tokens": 2,
    }).encode()
    
    req = urllib.request.Request(
        "https://api.deepseek.com/chat/completions",
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )
    try:
        resp = json.loads(urllib.request.urlopen(req, timeout=120).read())
        usage = resp.get("usage", {})
        pt = usage.get("prompt_tokens", "?")
        print(f"  {target_chars:>6} chars → OK (prompt_tokens={pt})", flush=True)
    except urllib.error.HTTPError as e:
        err = e.read().decode()[:300]
        print(f"  {target_chars:>6} chars → HTTP {e.code}: {err}", flush=True)
    except socket.timeout:
        print(f"  {target_chars:>6} chars → TIMEOUT", flush=True)
    except Exception as e:
        print(f"  {target_chars:>6} chars → {e}", flush=True)