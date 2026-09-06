#!/usr/bin/env python3
"""Generate the large-review fixtures — 3 files of ~800 lines of repeated
programming keywords, sized so each file is ~5.5K tokens (~22K chars).

Faithful to the original July-2026 nocache-gemma-large benchmark (files were
800 lines of random programming keywords; turn-1 prompt was 18,554 tokens).
Deterministic (fixed seed) so the run is reproducible.

Usage:
    python3 bench/gen_large_fixtures.py
"""

import os
import random

FIXTURE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "large-review")
KEYWORDS = [
    "api", "client", "server", "model", "route", "param", "function", "class",
    "cache", "import", "data", "request", "response", "endpoint", "handler",
    "middleware", "schema", "query", "index", "token", "session", "auth",
    "config", "deploy", "log", "metric", "worker", "queue", "stream", "async",
    "await", "callback", "promise", "buffer", "pipeline", "retry", "timeout",
    "header", "payload", "serialize", "parse", "validate", "migrate", "rollback",
]
LINES = 800
TARGET_CHARS_PER_FILE = 22_500  # ~5.5K tokens at ~4 chars/token


def gen_file(path: str, seed: int) -> None:
    rng = random.Random(seed)
    lines = []
    total = 0
    for _ in range(LINES):
        n = rng.randint(3, 10)
        line = " ".join(rng.choice(KEYWORDS) for _ in range(n))
        lines.append(line)
        total += len(line) + 1
    # Pad to target size with repeated filler lines
    filler = " ".join(rng.choice(KEYWORDS) for _ in range(40))
    while total < TARGET_CHARS_PER_FILE:
        lines.append(filler)
        total += len(filler) + 1
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")
    bytes_w = os.path.getsize(path)
    print(f"{os.path.basename(path)}: {len(lines)} lines, {bytes_w} bytes")


def main() -> None:
    os.makedirs(FIXTURE_DIR, exist_ok=True)
    for i in (1, 2, 3):
        gen_file(os.path.join(FIXTURE_DIR, f"large-file-{i}.txt"), seed=42 + i)


if __name__ == "__main__":
    main()