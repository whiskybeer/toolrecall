#!/usr/bin/env python3
"""Probe OpenRouter DeepSeek V4 Flash context limit at different sizes."""
import json, os, urllib.request, urllib.error, socket, sys

key = None
with open(os.path.expanduser("~/.hermes/.env")) as f:
    for line in f:
        line = line.strip()
        if "OPENROUTER_API_KEY" in line and "=" in line and not line.startswith("#"):
            key = line.split("=", 1)[1].strip().strip('"').strip("'")

for target_chars in [50000, 100000, 150000, 200000, 300000]:
    msg = "x" * target_chars  # ~0.25 tok/char
    body = json.dumps({
        "model": "deepseek/deepseek-v4-flash",
        "messages": [{"role": "user", "content": msg}],
        "max_tokens": 2,
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
        pt = usage.get("prompt_tokens", "?")
        print(f"  {target_chars:>6} chars → OK (prompt_tokens={pt})", flush=True)
    except urllib.error.HTTPError as e:
        err = e.read().decode()[:300]
        print(f"  {target_chars:>6} chars → HTTP {e.code}: {err}", flush=True)
    except socket.timeout:
        print(f"  {target_chars:>6} chars → TIMEOUT", flush=True)
    except Exception as e:
        print(f"  {target_chars:>6} chars → {e}", flush=True)