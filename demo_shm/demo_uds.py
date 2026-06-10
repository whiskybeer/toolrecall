#!/usr/bin/env python3
"""
Demo: Unix Domain Socket IPC.
Server stores cached values. Client requests them.
Every request = kernel syscalls + JSON serialize/deserialize.
"""

import json, os, socket, struct, time, sys, tempfile

SOCKET_PATH = tempfile.mktemp(suffix=".sock", prefix="uds_demo_")

def server_proc():
    """Run UDS server in a subprocess."""
    os.unlink(SOCKET_PATH) if os.path.exists(SOCKET_PATH) else None
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(SOCKET_PATH)
    server.listen(5)
    server.settimeout(0.5)

    # Simulated cache data
    cache = {
        "key:hello": {"content": "Hello World! " * 100, "mtime": 1000},
        "key:bigfile": {"content": "A" * 50000, "mtime": 1001},
        "key:data": {"content": json.dumps({"users": 1000, "status": "ok"}), "mtime": 1002},
    }

    try:
        while True:
            try:
                conn, _ = server.accept()
            except socket.timeout:
                continue
            data = b""
            while True:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                data += chunk
                if len(chunk) < 4096:
                    break
            if not data:
                conn.close()
                continue
            try:
                req = json.loads(data.decode())
                key = req.get("key", "")
                hit = cache.get(key)
                resp = {
                    "hit": hit is not None,
                    "content": hit["content"] if hit else None,
                    "latency_ns": 0,
                }
                conn.sendall(json.dumps(resp).encode() + b"\n")
            except Exception:
                pass
            conn.close()
    except SystemExit:
        pass
    finally:
        server.close()


def uds_request(key: str) -> dict:
    """Make a UDS request and measure full round-trip."""
    start = time.perf_counter_ns()
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.connect(SOCKET_PATH)
    req = json.dumps({"key": key}).encode()
    sock.sendall(req)
    data = b""
    while True:
        chunk = sock.recv(65536)
        if not chunk:
            break
        data += chunk
        if b"\n" in chunk:
            break
    sock.close()
    elapsed = time.perf_counter_ns() - start
    resp = json.loads(data.strip())
    resp["rtt_ns"] = elapsed
    return resp


if __name__ == "__main__":
    import multiprocessing
    print(f"[UDS] Socket at {SOCKET_PATH}")
    print(f"[UDS] Starting server...")
    proc = multiprocessing.Process(target=server_proc, daemon=True)
    proc.start()
    time.sleep(0.2)

    # Warm up
    uds_request("key:hello")

    keys = ["key:hello", "key:bigfile", "key:data"]
    for key in keys:
        results = []
        for _ in range(100):
            r = uds_request(key)
            results.append(r["rtt_ns"])
        avg = sum(results) / len(results)
        mn = min(results)
        mx = max(results)
        print(f"  {key:20s}  avg={avg/1000:8.1f}µs  min={mn/1000:6.1f}µs  max={mx/1000:8.1f}µs")

    proc.terminate()
    os.unlink(SOCKET_PATH) if os.path.exists(SOCKET_PATH) else None
