"""bench_common.py — shared workload for the media-dedup benchmark.

Deterministic browser-agent simulation: N pages, S steps each, one valid
1024x1024 solid-color PNG per page (distinct per page, byte-identical across
steps of the same page). Requests carry the full accumulated history.
"""

import base64
import struct
import zlib

PAGES = ["dashboard", "checkout", "settings"]
STEPS_PER_PAGE = 8
IMG_SIZE = 1024
PAGE_COLORS = {"dashboard": (40, 80, 160), "checkout": (160, 80, 40), "settings": (40, 160, 80)}

SYSTEM = (
    "You are a browser-use agent. Each message is one step observation "
    "(text + screenshot). Reply with ONE short line: your next action. No explanations."
)


def png_solid(w: int, h: int, rgb: tuple) -> bytes:
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


def page_data_url(page: str) -> str:
    png = png_solid(IMG_SIZE, IMG_SIZE, PAGE_COLORS[page])
    return "data:image/png;base64," + base64.b64encode(png).decode()


def build_session(seed: int) -> list:
    """Full step list for one seed. Page order rotates with the seed; text
    observations vary (cursor coords, step numbers) so seeds differ on the
    wire while image identity per page stays stable within a session."""
    pages = PAGES[seed % len(PAGES) :] + PAGES[: seed % len(PAGES)]
    msgs = [{"role": "system", "content": SYSTEM}]
    for i, page in enumerate(pages):
        for step in range(STEPS_PER_PAGE):
            msgs.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": f"Step {i * STEPS_PER_PAGE + step + 1} [seed {seed}]: "
                            f"page={page}, cursor=({10 + step * 3 + seed}, {20 + i * 7}), "
                            f"nav OK, 47 elements visible, no dialogs.",
                        },
                        {
                            "type": "image_url",
                            "image_url": {"url": page_data_url(page), "detail": "high"},
                        },
                    ],
                }
            )
    return msgs


def char_cost(messages: list) -> int:
    n = 0
    for m in messages:
        c = m.get("content")
        if isinstance(c, str):
            n += len(c)
        elif isinstance(c, list):
            for p in c:
                if p.get("type") == "text":
                    n += len(p.get("text", ""))
                elif p.get("type") == "image_url":
                    n += len(p["image_url"]["url"])
    return n
