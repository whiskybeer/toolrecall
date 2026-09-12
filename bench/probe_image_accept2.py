"""probe_image_accept2.py — the real stub datapoint.

Builds a conversation where an identical image dup sits OUTSIDE the
protect_last tail, verifies the stub fires, then asks the provider to price
both variants: full image vs stub-in-image_url part.
"""

import base64
import json
import re
import struct
import sys
import urllib.request
import zlib

sys.path.insert(0, "/home/hermes/toolrecall")
from toolrecall.adapters.litellm import dedup_messages  # noqa: E402


def png_solid(w, h, rgb):
    raw = b"".join(b"\x00" + bytes(rgb) * w for _ in range(h))

    def chunk(tag, data):
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


def main():
    png = png_solid(1024, 1024, (40, 80, 160))
    url = "data:image/png;base64," + base64.b64encode(png).decode()
    print(f"PNG 1024x1024: {len(png)} bytes")

    svc = open("/home/hermes/.config/systemd/user/toolrecall-edge.service").read()
    key = re.search(r"sk-or-v1-[0-9a-f]{64}", svc).group(0)

    def call(messages):
        body = json.dumps(
            {"model": "z-ai/glm-5.3-flash", "messages": messages, "max_tokens": 24, "temperature": 0}
        ).encode()
        req = urllib.request.Request(
            "https://openrouter.ai/api/v1/chat/completions",
            data=body,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r)

    def step(text):
        return {
            "role": "user",
            "content": [
                {"type": "text", "text": text},
                {"type": "image_url", "image_url": {"url": url, "detail": "high"}},
            ],
        }

    # [sys, img, img-dup, filler, filler] — dup at index 2, tail 3-4 text-only
    msgs = [
        {"role": "system", "content": "One short line: your next action."},
        step("Step 1: dashboard loaded."),
        step("Step 2: still on dashboard."),
        {"role": "user", "content": [{"type": "text", "text": "Step 3: clicked settings."}]},
        {"role": "user", "content": [{"type": "text", "text": "Step 4: settings open."}]},
    ]

    out, stats = dedup_messages(json.loads(json.dumps(msgs)), min_chars=800, protect_last=2, media=True)
    print(f"stubs applied: {stats['blocks']} (expect 1)")
    assert stats["blocks"] == 1, "stub must fire outside the tail"

    r_full = call(msgs)
    r_stub = call(out)
    uf, us = r_full.get("usage", {}), r_stub.get("usage", {})
    if not r_full.get("choices") or not r_stub.get("choices"):
        print("FULL:", json.dumps(r_full)[:300])
        print("STUB:", json.dumps(r_stub)[:300])
        return 1
    print(f"full image : prompt={uf['prompt_tokens']} cost=${uf['cost']}")
    print(f"stubbed    : prompt={us['prompt_tokens']} cost=${us['cost']}")
    print(f"delta      : {uf['prompt_tokens'] - us['prompt_tokens']} prompt tokens, ${uf['cost'] - us['cost']:.6f}")
    print("STUB-IN-IMAGE_URL ACCEPTED BY PROVIDER: YES")
    return 0


if __name__ == "__main__":
    sys.exit(main())
