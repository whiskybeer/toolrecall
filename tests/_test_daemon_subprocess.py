"""Minimal UDS daemon for integration testing."""
import os
import socket
import json
import tempfile

UDS = os.environ.get("TOOLRECALL_UDS_PATH", os.path.join(tempfile.gettempdir(), "tc_test.sock"))
CACHE_DB = os.environ.get("TOOLRECALL_CACHE_DB", os.path.join(tempfile.gettempdir(), "test_cache.db"))

os.makedirs(os.path.dirname(UDS), exist_ok=True)
try:
    os.unlink(UDS)
except OSError:
    pass

sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
sock.bind(UDS)
sock.listen(5)
sock.settimeout(5.0)

while True:
    try:
        c, _ = sock.accept()
        data = c.recv(4096)
        if data:
            req = json.loads(data.decode())
            if req.get("action") == "ping":
                c.sendall(json.dumps({"status": "pong"}).encode())
            elif req.get("action") == "stats":
                c.sendall(json.dumps({"hits": 42, "misses": 7, "tokens_saved": 99999}).encode())
            elif req.get("action") == "invalidate":
                c.sendall(json.dumps({"status": "ok", "cleared": True}).encode())
            else:
                c.sendall(json.dumps({"error": "unknown_action"}).encode())
        c.close()
    except socket.timeout:
        pass