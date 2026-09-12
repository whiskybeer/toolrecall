"""arm_worker.py — one benchmark arm run inside its own venv.

Usage: python arm_worker.py <arm: naive|toolrecall> <seed> <out.jsonl>

Runs the full session for one seed turn-by-turn (full accumulated history
per request) and appends one JSON line per request to <out.jsonl>:
  {arm, seed, turn, usage{...verbatim provider billing...}, chars_sent, stubs}

The toolrecall arm routes every request through dedup_messages(media=True)
before sending. The naive arm sends raw history. Identical turn semantics
otherwise (same model, temperature, max_tokens).
"""

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

BENCH_DIR = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(BENCH_DIR)
sys.path.insert(0, BENCH_DIR)

from bench_common import build_session, char_cost  # noqa: E402

MODEL = "z-ai/glm-5.3-flash"
OR_URL = "https://openrouter.ai/api/v1/chat/completions"


def load_key(arm: str) -> str:
    if arm == "toolrecall":
        svc = os.path.expanduser("~/.config/systemd/user/toolrecall-edge.service")
        m = re.search(r"sk-or-v1-[0-9a-f]{64}", open(svc).read())
        if not m:
            raise SystemExit("edge key not found")
        return m.group(0)
    env_path = os.path.expanduser("~/.hermes/.env")
    for line in open(env_path):
        if line.startswith("OPENROUTER_API_KEY") and "=" in line:
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise SystemExit("main key not found")


def call(key: str, messages: list) -> dict:
    body = json.dumps(
        {"model": MODEL, "messages": messages, "max_tokens": 32, "temperature": 0}
    ).encode()
    req = urllib.request.Request(
        OR_URL,
        data=body,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            detail = e.read().decode()[:200]
            if e.code in (429, 500, 502, 503) and attempt < 3:
                time.sleep(3 * (attempt + 1))
                continue
            raise RuntimeError(f"HTTP {e.code}: {detail}")
        except Exception:
            if attempt < 3:
                time.sleep(3 * (attempt + 1))
                continue
            raise


def usage_of(resp: dict) -> dict:
    u = dict(resp.get("usage") or {})
    det = u.pop("prompt_tokens_details", None)
    if isinstance(det, dict):
        u["prompt_cached_tokens"] = det.get("cached_tokens")
    return u


def main():
    arm, seed, out_path = sys.argv[1], int(sys.argv[2]), sys.argv[3]
    key = load_key(arm)

    if arm == "toolrecall":
        from toolrecall.adapters.litellm import dedup_messages

        def process(view):
            return dedup_messages(
                json.loads(json.dumps(view)), min_chars=800, protect_last=2, media=True
            )

    else:

        def process(view):
            return view, {"blocks": 0}

    session = build_session(seed)
    n_steps = len(session) - 1
    with open(out_path, "a") as f:
        for t in range(1, n_steps + 1):
            view = session[: 1 + t]
            msgs, stats = process(view)
            resp = call(key, msgs)
            u = usage_of(resp)
            rec = {
                "arm": arm,
                "seed": seed,
                "turn": t,
                "usage": u,
                "chars_sent": char_cost(msgs),
                "stubs": stats["blocks"],
            }
            f.write(json.dumps(rec) + "\n")
            f.flush()
            print(
                f"[{arm} s{seed} t{t:02d}] prompt={u.get('prompt_tokens')} "
                f"cached={u.get('prompt_cached_tokens')} cost=${u.get('cost')}",
                flush=True,
            )


if __name__ == "__main__":
    main()
