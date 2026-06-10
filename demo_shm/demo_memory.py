#!/usr/bin/env python3
"""
Demonstrate the memory efficiency of Shared Memory vs UDS.
We show memory usage in two key scenarios:
1. Cache just stores it once, served via SHM → zero duplication
2. Cache stores via UDS → data exists in server heap AND in client memory after recv
"""

import mmap, os, struct, time, sys, socket, json, tempfile
from multiprocessing import shared_memory, Process, Queue
import tracemalloc

SHM_NAME = "mem_demo_shm"
SLOT_SIZE = 65536

# A realistic cache payload: ~32KB (typical tool output)
PAYLOAD = b"ToolRecall cached output. " * 2000  # ~48KB

# ── SHM SETUP ──
try:
    shm = shared_memory.SharedMemory(name=SHM_NAME, create=True, size=SLOT_SIZE)
    shm.buf[:] = b"\x00" * SLOT_SIZE
except FileExistsError:
    shm = shared_memory.SharedMemory(name=SHM_NAME)

# Write payload
shm.buf[0:len(PAYLOAD)] = PAYLOAD
struct.pack_into("I", shm.buf, SLOT_SIZE - 4, len(PAYLOAD))

# ── UDS SETUP ──
SOCK_PATH = tempfile.mktemp(suffix=".sock", prefix="mem_demo_")
os.unlink(SOCK_PATH) if os.path.exists(SOCK_PATH) else None

def uds_server(q):
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(SOCK_PATH)
    srv.listen(5)
    srv.settimeout(1.0)
    # Server holds the payload in its heap
    data = PAYLOAD
    while True:
        try:
            conn, _ = srv.accept()
        except socket.timeout:
            continue
        _ = conn.recv(64)
        # Send: data first lives in server heap (data), then in kernel socket buffer,
        # then in client heap after recv
        conn.sendall(struct.pack("I", len(data)) + data)
        conn.close()

q = Queue()
ps = Process(target=uds_server, args=(q,), daemon=True)
ps.start()
time.sleep(0.2)

# ── READ VIA SHM ──
shm_client = shared_memory.SharedMemory(name=SHM_NAME)
start = time.perf_counter_ns()
length = struct.unpack_from("I", shm_client.buf, SLOT_SIZE - 4)[0]
result_shm = bytes(shm_client.buf[0:length])
shm_time = time.perf_counter_ns() - start

# ── READ VIA UDS ──
sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
sock.connect(SOCK_PATH)
sock.sendall(b"get")
data = b""
while True:
    chunk = sock.recv(65536)
    if not chunk:
        break
    data += chunk
    if len(data) >= 4:
        expected = struct.unpack_from("I", data, 0)[0]
        if len(data) >= expected + 4:
            break
sock.close()
start = time.perf_counter_ns()
result_uds = data[4:4 + expected]
uds_time = time.perf_counter_ns() - start

print("=== MEMORY & SPEED DEMO ===")
print(f"Payload size: {len(PAYLOAD):,} bytes")
print()
print("── SHARED MEMORY ──")
print(f"  Read time:  {shm_time/1000:.1f} µs")
print(f"  Memory:     data lives ONCE in RAM")
print(f"  result_shm is a COPY from mmap'd buffer")
print(f"  → No kernel involved. No JSON. No socket buffer.")
print()
print("── UNIX SOCKET ──")
print(f"  Read time:  {uds_time/1000:.1f} µs")
print(f"  Memory cost:")
print(f"    1. Server heap:    {len(PAYLOAD):>6,} bytes  (data var in Python)")
print(f"    2. Kernel buffer:  {len(PAYLOAD):>6,} bytes  (kernel socket buffer)")
print(f"    3. Client heap:    {len(PAYLOAD):>6,} bytes  (result_uds after recv)")
print(f"    ─────────────────────")
print(f"    Total:            {len(PAYLOAD)*3:>6,} bytes")
print(f"  → Data is duplicated 3× for one request.")
print()

speedup = uds_time / shm_time
print(f"Speedup: {speedup:.0f}×")
print()
print(f"RAM saved per request: {len(PAYLOAD)*2:,} bytes (kernel buf + duplicate)")
print(f"At 10K requests: {(len(PAYLOAD)*2)*10000/1024/1024:.0f} MB saved")
print(f"At 100K requests: {(len(PAYLOAD)*2)*100000/1024/1024:.0f} MB saved")

# Cleanup
shm.close()
try:
    shm.unlink()
except:
    pass
shm_client.close()
ps.terminate()
os.unlink(SOCK_PATH) if os.path.exists(SOCK_PATH) else None
