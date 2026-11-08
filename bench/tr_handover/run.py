#!/usr/bin/env python3
"""W1 arm harness — G3 (billing attribution) and G4 (paired mini-run).

Both gates exist to answer one question before any money is spent at scale:
when the bridge stubs a repeated read, do the billed prompt tokens actually
drop, and can we attribute every call to exactly one arm?

  G3  isolation smoke: 3 turns in arm A -> exactly 3 new rows in A's CSV,
      none anywhere else. Stop if not: a shared usage log invalidates the
      whole measurement.
  G4  paired 6-turn mini-run: stub-on prompt tokens <= stub-off, with both
      arms sending byte-identical conversations except the tool-result
      rendering. ~$0.05.

Workload is deterministic: two fixed 6 KB fixtures, read A,B,A,B,A,B. Repeats
are what the stub layer acts on; the re-read rate here (4 of 6) is deliberately
ABOVE the census rate (8.8%) — this is a direction check, not the headline.

Each arm gets its own HOME (so its own ~/.toolrecall/proxy_usage.csv), its own
daemon socket, its own cache DB and its own proxy port. Billed numbers are read
from that CSV, never from the API response.

Usage:
    python3 bench/tr_handover/run.py g3
    python3 bench/tr_handover/run.py g4
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, REPO)

MODEL = "deepseek/deepseek-v4.1-flash-1017"
BENCH_ROOT = os.path.expanduser("~/bench-data/tr-handover")
FIXTURE_BYTES = 6000
TURNS = ("a.py", "b.py", "a.py", "b.py", "a.py", "b.py")


def load_openrouter_key() -> str:
    """OpenRouter key from Hermes' env file (or the process env). Never printed."""
    key = (os.environ.get("OPENROUTER_API_KEY") or "").strip()
    if key:
        return key
    env_path = os.path.expanduser("~/.hermes/.env")
    if os.path.exists(env_path):
        with open(env_path, errors="ignore") as f:
            for line in f:
                line = line.strip()
                if line.startswith("OPENROUTER_API_KEY="):
                    value = line.split("=", 1)[1].strip().strip('"').strip("'")
                    if value:
                        return value
    raise SystemExit("no OPENROUTER_API_KEY in ~/.hermes/.env or the environment")


def make_fixtures(root: str) -> dict[str, str]:
    os.makedirs(root, exist_ok=True)
    out = {}
    for name, accent in (("a.py", "alpha"), ("b.py", "beta")):
        body = "".join(
            f"{name.upper()[:5]}_{i} = '{accent} payload line {i}'\n"
            for i in range(FIXTURE_BYTES // 40)
        )
        path = os.path.join(root, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write(body)
        out[name] = path
    return out


class Arm:
    """One arm: own HOME, socket, cache DB, proxy port, daemon."""

    def __init__(self, run_dir: str, name: str, port: int, stub: bool, fixtures_dir: str):
        self.name = name
        self.home = os.path.join(run_dir, name)
        self.port = port
        self.stub = stub
        self.fixtures_dir = fixtures_dir
        os.makedirs(self.home, exist_ok=True)
        self.sock = os.path.join(self.home, "tr.sock")
        self.db = os.path.join(self.home, f"trc-handover-{name}.db")
        self.proc = None

    @property
    def csv_path(self) -> str:
        return os.path.join(self.home, ".toolrecall", "proxy_usage.csv")

    def env(self) -> dict:
        env = dict(os.environ)
        env.update(
            {
                "HOME": self.home,
                "XDG_CONFIG_HOME": os.path.join(self.home, ".config"),
                "TOOLRECALL_TRANSPORT": self.sock,
                "TOOLRECALL_CACHE_DB": self.db,
                "TOOLRECALL_FORWARD_PORT": str(self.port),
                "TOOLRECALL_NO_AUTOSTART": "1",
                "TOOLRECALL_AGENT_ID": self.name,
                "TOOLRECALL_BRIDGE_STUB": "1" if self.stub else "0",
            }
        )
        return env

    def start(self):
        # The daemon reads its config from Path.home() (this arm's HOME), and
        # default-denies every path without an allowlist entry. Both arms get
        # the same fixture dir, so the conversation paths are byte-identical.
        cfg_dir = os.path.join(self.home, ".config", "toolrecall")
        os.makedirs(cfg_dir, exist_ok=True)
        with open(os.path.join(cfg_dir, "toolrecall.toml"), "w", encoding="utf-8") as f:
            f.write("[mcp]\n")
            f.write(
                "allowed_paths = [%s, %s]\n"
                % (json.dumps(self.fixtures_dir), json.dumps(self.home))
            )
        log = open(os.path.join(self.home, "daemon.log"), "w")
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "toolrecall", "daemon", "--foreground"],
            cwd=REPO,
            env=self.env(),
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        deadline = time.time() + 20
        while time.time() < deadline:
            try:
                s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                s.settimeout(0.5)
                s.connect(self.sock)
                s.close()
                return
            except OSError:
                time.sleep(0.2)
        raise SystemExit(f"{self.name}: daemon did not come up (see {self.home}/daemon.log)")

    def stop(self):
        if self.proc is None:
            return
        try:
            self.proc.terminate()
            self.proc.wait(timeout=10)
        except Exception:
            self.proc.kill()

    def csv_rows(self) -> list[dict]:
        if not os.path.exists(self.csv_path):
            return []
        import csv as _csv

        with open(self.csv_path, newline="") as f:
            return list(_csv.DictReader(f))

    def chat(self, messages: list[dict], max_tokens: int = 8) -> dict:
        """One billed call through THIS arm's proxy."""
        body = json.dumps(
            {"model": MODEL, "messages": messages, "max_tokens": max_tokens, "temperature": 0}
        ).encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/v1/chat/completions",
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {load_openrouter_key()}",
                "X-Target-Host": "openrouter.ai",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            raise SystemExit(f"{self.name}: proxy HTTP {e.code}: {e.read()[:300]!r}")


def num(row: dict, key: str) -> int:
    try:
        return int(float(row.get(key) or 0))
    except (TypeError, ValueError):
        return 0


def read_turn(bridge, path: str) -> tuple[str, bool]:
    """One bridge read. Returns (wire text, was_stub)."""
    resp = bridge.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "read_file", "arguments": {"path": path}},
        }
    )
    assert resp is not None and "error" not in resp, resp
    text = resp["result"]["content"][0]["text"]
    try:
        obj = json.loads(text)
        body = obj.get("result", obj)
        return text, bool(isinstance(body, dict) and body.get("unchanged") is True)
    except json.JSONDecodeError:
        return text, False


def run_arm(arm: Arm, fixtures: dict, turns: int, key: str) -> list[dict]:
    from toolrecall.mcp_bridge import MCPBridge

    os.environ["TOOLRECALL_BRIDGE_STUB"] = "1" if arm.stub else "0"
    bridge = MCPBridge(arm.sock, emit_context_hints=True)
    messages = [{"role": "system", "content": "You are a terse harness. Reply with OK."}]
    rows = []
    for i in range(turns):
        name = TURNS[i]
        text, stubbed = read_turn(bridge, fixtures[name])
        messages.append({"role": "user", "content": f"TOOL RESULT {name}:\n{text}"})
        before = len(arm.csv_rows())
        arm.chat(messages)
        # The proxy writes the billed row off the response path, so poll for it
        # briefly instead of racing it — but never accept more than one row.
        deadline = time.time() + 3.0
        after = arm.csv_rows()
        while len(after) < before + 1 and time.time() < deadline:
            time.sleep(0.05)
            after = arm.csv_rows()
        assert len(after) == before + 1, f"{arm.name}: expected exactly 1 new CSV row, got {len(after) - before}"
        row = after[-1]
        rows.append(
            {
                "turn": i + 1,
                "file": name,
                "stub": stubbed,
                "prompt_tokens": num(row, "prompt_tokens"),
                "cache_read_tokens": num(row, "cache_read_tokens"),
                "cost_usd": float(row.get("cost_usd") or 0),
                "wire_chars": len(text),
            }
        )
    os.environ.pop("TOOLRECALL_BRIDGE_STUB", None)
    return rows


def print_rows(name: str, rows: list[dict]):
    print(f"\n{name}:")
    print(f"  {'turn':>4} {'file':>6} {'stub':>5} {'prompt':>8} {'cache_rd':>9} {'cost$':>8} {'wire_chars':>10}")
    for r in rows:
        print(
            f"  {r['turn']:>4} {r['file']:>6} {str(r['stub']):>5} "
            f"{r['prompt_tokens']:>8} {r['cache_read_tokens']:>9} "
            f"{r['cost_usd']:>8.5f} {r['wire_chars']:>10}"
        )
    print(
        f"  TOTAL prompt={sum(r['prompt_tokens'] for r in rows)} "
        f"cache_read={sum(r['cache_read_tokens'] for r in rows)} "
        f"cost=${sum(r['cost_usd'] for r in rows):.5f}"
    )


def cmd_g3(args) -> int:
    run = os.path.join(BENCH_ROOT, f"g3-{time.strftime('%Y%m%d-%H%M%S')}")
    fixtures_dir = os.path.join(run, "files")
    fixtures = make_fixtures(fixtures_dir)
    arms = {
        "stubon": Arm(run, "stubon", 18601, True, fixtures_dir),
        "stuboff": Arm(run, "stuboff", 18602, False, fixtures_dir),
    }
    try:
        for arm in arms.values():
            arm.start()
        rows = run_arm(arms["stubon"], fixtures, turns=3, key=load_openrouter_key())
        print_rows("stubon (3 calls)", rows)

        a = len(arms["stubon"].csv_rows())
        b = len(arms["stuboff"].csv_rows())
        print(f"\nCSV rows: stubon={a} (expected 3), stuboff={b} (expected 0)")
        ok = a == 3 and b == 0
        print(f"G3: {'PASS' if ok else 'FAIL'} — billing attribution is per-arm")
        return 0 if ok else 1
    finally:
        for arm in arms.values():
            arm.stop()


def cmd_g4(args) -> int:
    run = os.path.join(BENCH_ROOT, f"g4-{time.strftime('%Y%m%d-%H%M%S')}")
    fixtures_dir = os.path.join(run, "files")
    fixtures = make_fixtures(fixtures_dir)
    arms = {
        "off": Arm(run, "stuboff", 18611, False, fixtures_dir),
        "on": Arm(run, "stubon", 18612, True, fixtures_dir),
    }
    try:
        for arm in arms.values():
            arm.start()
        off_rows = run_arm(arms["off"], fixtures, turns=6, key=load_openrouter_key())
        on_rows = run_arm(arms["on"], fixtures, turns=6, key=load_openrouter_key())
        print_rows("stuboff", off_rows)
        print_rows("stubon", on_rows)

        off_total = sum(r["prompt_tokens"] for r in off_rows)
        on_total = sum(r["prompt_tokens"] for r in on_rows)
        delta = off_total - on_total
        pct = (100.0 * delta / off_total) if off_total else 0.0
        print(
            f"\nprompt tokens: stuboff={off_total} stubon={on_total} "
            f"delta={delta} ({pct:.1f}%)"
        )
        print(
            f"stubs fired: {sum(1 for r in on_rows if r['stub'])}/6 in stubon, "
            f"{sum(1 for r in off_rows if r['stub'])}/6 in stuboff"
        )
        ok = on_total <= off_total and sum(1 for r in on_rows if r["stub"]) > 0
        print(f"G4: {'PASS' if ok else 'FAIL'} — stub-on must not cost more, and must fire")
        return 0 if ok else 1
    finally:
        for arm in arms.values():
            arm.stop()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("gate", choices=["g3", "g4"])
    args = ap.parse_args()
    return cmd_g3(args) if args.gate == "g3" else cmd_g4(args)


if __name__ == "__main__":
    raise SystemExit(main())
