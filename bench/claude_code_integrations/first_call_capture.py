#!/usr/bin/env python3
"""Why do ~13% of identical headless reruns miss at call 1?
Capture the first /v1/messages body of N identical `claude -p` runs (capture server answers 400,
nothing reaches the API), compute the proxy's claude-code canonical key, and diff variants.
Usage: first_call_capture.py <repo> <n>"""
import hashlib, http.server, json, os, shutil, subprocess, sys, threading
from pathlib import Path

REPO, N = sys.argv[1], int(sys.argv[2])
sys.path.insert(0, REPO)
from toolrecall.proxy import _canonicalize_body  # noqa: E402

OUT = Path(os.environ.get("BENCH_DIR", "/tmp/tr-cc-bench")) / "first_call"; OUT.mkdir(parents=True, exist_ok=True)
W = OUT / "work"
bodies = []


class H(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        b = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        if self.path.split("?")[0].endswith("/v1/messages"):
            bodies.append(b)
        self.send_response(400)
        self.send_header("content-type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"type":"error","error":{"type":"invalid_request_error","message":"capture"}}')

    def log_message(self, *a):
        pass


srv = http.server.ThreadingHTTPServer(("127.0.0.1", 8598), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()

keys = []
for i in range(N):
    if W.exists():
        shutil.rmtree(W)
    W.mkdir(parents=True)
    (W / "stats.py").write_text("def mean(xs):\n    return sum(xs) / len(xs) - 1\n")
    env = {k: v for k, v in os.environ.items() if k not in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SESSION_ID")}
    env.update(ANTHROPIC_BASE_URL="http://127.0.0.1:8598",
               ANTHROPIC_CUSTOM_HEADERS="X-ToolRecall-Canonicalize: claude-code", PYTHONHASHSEED="0")
    n0 = len(bodies)
    subprocess.run([shutil.which("claude") or "claude", "-p", "Fix the bug in stats.py. Reply with one line.",
                    "--model", "haiku", "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
                    "--allowedTools", "Read,Edit", "--output-format", "json"],
                   cwd=W, env=env, capture_output=True, timeout=120, stdin=subprocess.DEVNULL)
    mine = bodies[n0:]
    if not mine:
        print(i, "no request captured"); continue
    first = mine[0]
    (OUT / f"run{i:02d}.json").write_bytes(first)
    canon = _canonicalize_body("claude-code", first)
    k = hashlib.sha256(canon).hexdigest()[:12]
    keys.append(k)
    print(i, "requests", len(mine), "key", k, flush=True)
srv.shutdown()

from collections import Counter  # noqa: E402
print("distinct canonical keys:", Counter(keys))
