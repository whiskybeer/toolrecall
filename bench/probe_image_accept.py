"""probe_image_accept.py — two single-call datapoints before the live benchmark.

P1: a valid PNG (solid color, 512x512) accepted by z-ai/glm-5.3-flash?
P2: dedup-ON semantics — an image part whose URL was replaced by the
    toolrecall stub text — accepted, or rejected as malformed?
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
    png = png_solid(512, 512, (40, 80, 160))
    url = "data:image/png;base64," + base64.b64encode(png).decode()
    print(f"PNG: {len(png)} bytes, data-url {len(url)} chars")

    svc = open("/home/hermes/.config/systemd/user/toolrecall-edge.service").read()
    key = re.search(r"sk-or-v1-[0-9a-f]{64}", svc).group(0)

    def call(messages):
        body = json.dumps(
            {
                "model": "z-ai/glm-5.3-flash",
                "messages": messages,
                "max_tokens": 24,
                "temperature": 0,
            }
        ).encode()
        req = urllib.request.Request(
            "https://openrouter.ai/api/v1/chat/completions",
            data=body,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r)

    img_part = {"type": "image_url", "image_url": {"url": url, "detail": "high"}}
    base = [
        {"role": "system", "content": "One short line: your next action."},
        {"role": "user", "content": [{"type": "text", "text": "Step 1: dashboard loaded."}, img_part]},
    ]

    r1 = call(base)
    if r1.get("choices"):
        print("P1 OK - usage:", json.dumps(r1["usage"])[:240])
    else:
        print("P1 ERROR BODY:", json.dumps(r1)[:400])
        return 1

    msgs2 = json.loads(json.dumps(base))
    msgs2.append(json.loads(json.dumps(base[1])))
    out, stats = dedup_messages(msgs2, min_chars=800, protect_last=2, media=True)
    print(f"P2 stubs applied: {stats['blocks']}")
    r2 = call(out)
    if r2.get("choices"):
        print("P2 OK (stub-in-image_url accepted) - usage:", json.dumps(r2["usage"])[:240])
    else:
        print("P2 ERROR BODY:", json.dumps(r2)[:400])
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
