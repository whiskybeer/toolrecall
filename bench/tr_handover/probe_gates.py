#!/usr/bin/env python3
"""W1 gates G0-G2 — does the MCP bridge stub a second read? (plan v2, free probes)

These three probes run BEFORE any paid arm. They answer one question the whole
W1 measurement rests on: when the bridge's session has already sent a file's
content to the agent, does the next read of that unchanged file get replaced
by a stub, and does a COLD session still get full content?

  G0  bridge second read returns a stub in an isolated instance.
  G1  the stub fires while the earlier full copy is still in the transcript,
      even with an unrelated read in between (transcript growth, not idleness).
  G2  a cold handover (fresh bridge session, no prior read) gets FULL content.
      Stop if G2 fails: the stub would be claiming content the agent never saw.

Isolation: each gate owns a temp UDS socket, a temp "daemon" that serves file
bytes, and its own MCPBridge instance. No real daemon, no cache DB, no API
spend. The daemon is a stub — this validates the bridge's stub DECISION path
and its wire shape, not the disk cache (that is covered by G3+ later).

Usage:
    python3 bench/tr_handover/probe_gates.py [-v]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from toolrecall.mcp_bridge import MCPBridge  # noqa: E402
from toolrecall.transport import (  # noqa: E402
    bind_socket,
    create_socket,
    receive_message,
    send_message,
)


class FakeDaemon:
    """Tiny UDS daemon: serves file bytes by path and records every request."""

    def __init__(self, socket_path: str, files: dict[str, str]):
        self.path = socket_path
        self.files = files
        self.requests: list[dict] = []
        self.server = create_socket(socket_path)
        bind_socket(self.server, socket_path)
        self.server.listen(8)
        self._stop = False
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()
        time.sleep(0.05)

    def _serve(self):
        while not self._stop:
            try:
                conn, _ = self.server.accept()
            except OSError:
                return
            try:
                while True:
                    req = receive_message(conn)
                    if req is None:
                        break
                    self.requests.append(req)
                    send_message(conn, self._handle(req))
            except Exception:
                pass
            finally:
                try:
                    conn.close()
                except OSError:
                    pass

    def _handle(self, req: dict) -> dict:
        cmd = req.get("cmd", "")
        if cmd == "ping":
            return {
                "allowed_paths": ["/tmp", "/home"],
                "allow_terminal": True,
                "allow_invalidate": True,
                "multiplex_enabled": True,
                "recall_enabled": True,
            }
        if cmd in ("cached_read", "read_file", "cache_refresh_file"):
            p = req.get("path", "")
            body = self.files.get(p)
            if body is None:
                return {"error": f"file not found: {p}"}
            return {
                "result": {
                    "path": p,
                    "content": body,
                    "cached": True,
                    "lossless": True,
                    "size": len(body),
                }
            }
        if cmd == "dedup_tokens":
            return {"result": {"ok": True}}
        if cmd == "update_pending_hint":
            return {"hint": ""}
        return {"result": {"echo": cmd}}

    def stop(self):
        self._stop = True
        try:
            self.server.close()
        except OSError:
            pass
        try:
            os.unlink(self.path)
        except FileNotFoundError:
            pass


class Session:
    """One bridge session = one MCP client transcript."""

    def __init__(self, daemon: FakeDaemon):
        self.bridge = MCPBridge(daemon.path, emit_context_hints=False)
        self.transcript: list[str] = []  # text each tool result put on the wire
        self._id = 0
        self.bridge.handle_request(
            {
                "jsonrpc": "2.0",
                "id": 0,
                "method": "initialize",
                "params": {"clientInfo": {"name": "w1-probe", "version": "0"}},
            }
        )

    def read(self, path: str) -> str:
        self._id += 1
        resp = self.bridge.handle_request(
            {
                "jsonrpc": "2.0",
                "id": self._id,
                "method": "tools/call",
                "params": {"name": "read_file", "arguments": {"path": path}},
            }
        )
        assert resp is not None and "error" not in resp, resp
        text = resp["result"]["content"][0]["text"]
        self.transcript.append(text)
        return text


def is_stub(text: str) -> bool:
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        return False
    body = obj.get("result", obj)
    return bool(isinstance(body, dict) and body.get("unchanged") is True)


def has_content(text: str, content: str) -> bool:
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        return content in text
    body = obj.get("result", obj)
    return isinstance(body, dict) and body.get("content") == content


def transcript_holds(transcript: list[str], content: str) -> bool:
    """True if any earlier tool result carried exactly ``content``.

    Wire text is JSON, so newlines are escaped — a raw substring check would
    report "not in transcript" for content that is plainly there.
    """
    return any(has_content(t, content) for t in transcript)


def gate(name: str, ok: bool, detail: str, verbose: bool, extra: str = "") -> bool:
    print(f"{name}: {'PASS' if ok else 'FAIL'} — {detail}")
    if verbose and extra:
        print(extra)
    return ok


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    tmp = tempfile.mkdtemp(prefix="w1-gates-")
    a_path = os.path.join(tmp, "alpha.py")
    b_path = os.path.join(tmp, "beta.py")
    a_body = "ALPHA = 1\n" + "x = 'alpha content line'\n" * 80
    b_body = "BETA = 2\n" + "y = 'beta content line'\n" * 60
    files = {a_path: a_body, b_path: b_body}

    results: dict[str, bool] = {}

    # ── G0: second identical read in one session ─────────────────────────
    d0 = FakeDaemon(os.path.join(tmp, "g0.sock"), files)
    try:
        s0 = Session(d0)
        r1 = s0.read(a_path)
        r2 = s0.read(a_path)
        first_full = has_content(r1, a_body)
        second_stub = is_stub(r2)
        results["G0"] = gate(
            "G0",
            first_full and second_stub,
            f"read1 full={first_full}, read2 stub={second_stub}",
            args.verbose,
            extra=f"  read2 wire text: {r2[:160]}",
        )
    finally:
        d0.stop()

    # ── G1: stub still fires with an unrelated read in between ───────────
    d1 = FakeDaemon(os.path.join(tmp, "g1.sock"), files)
    try:
        s1 = Session(d1)
        ra1 = s1.read(a_path)   # full copy of A enters the transcript
        rb1 = s1.read(b_path)   # unrelated read; transcript grows
        ra2 = s1.read(a_path)   # A unchanged -> stub
        # Check the transcript as it stood BEFORE the stub request.
        in_transcript = transcript_holds(s1.transcript[:-1], a_body)
        dedup = [r for r in d1.requests if r.get("cmd") == "dedup_tokens"]
        counted = sum(int(r.get("tokens", 0)) for r in dedup)
        results["G1"] = gate(
            "G1",
            in_transcript and has_content(ra1, a_body) and has_content(rb1, b_body)
            and is_stub(ra2),
            f"full A then full B then A stub={is_stub(ra2)}; "
            f"earlier copy in transcript={in_transcript}; dedup_tokens={counted}",
            args.verbose,
            extra=f"  dedup requests: {dedup}",
        )
    finally:
        d1.stop()

    # ── G2: cold session must NOT stub ───────────────────────────────────
    d2 = FakeDaemon(os.path.join(tmp, "g2.sock"), files)
    try:
        s2 = Session(d2)  # fresh handover: this transcript never saw A
        r_first = s2.read(a_path)
        cold_full = has_content(r_first, a_body) and not is_stub(r_first)
        results["G2"] = gate(
            "G2",
            cold_full,
            f"first read in a fresh session is full content={cold_full}",
            args.verbose,
            extra=f"  wire text: {r_first[:160]}",
        )
    finally:
        d2.stop()

    failed = [k for k, v in results.items() if not v]
    print()
    if failed:
        print(f"STOP: {', '.join(failed)} failed — do not start W1 arms.")
        return 1
    print("All three gates passed — W1 arm logic may proceed (G3 needs a real daemon).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
