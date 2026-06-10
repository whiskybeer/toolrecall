#!/usr/bin/env python3
"""
Scale test: UDS vs SHM at different payload sizes.
Shows that SHM stays flat while UDS grows linearly.
"""

import json, os, socket, struct, time, tempfile, multiprocessing
from multiprocessing import shared_memory

SOCKET_PATH = tempfile.mktemp(suffix=".sock", prefix="uds_scale_")
SHM_NAME = "toolrecall_scale_shm"
SLOT_SIZE = 1048576  # 1 MB per slot

# --- UDS Server ---
def uds_server():
    os.unlink(SOCKET_PATH) if os.path.exists(SOCKET_PATH) else None
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(SOCKET_PATH)
    server.listen(5)
    server.settimeout(0.5)
    try:
        while True:
            try:
                conn, _ = server.accept()
            except socket.timeout:
                continue
            data = b""
            while True:
                chunk = conn.recv(65536)
                if not chunk:
                    break
                data += chunk
                if len(chunk) < 65536:
                    break
            if not data:
                conn.close()
                continue
            try:
                req = json.loads(data.decode())
                size = req.get("size", 1000)
                resp = {"data": "x" * size}
                conn.sendall(json.dumps(resp).encode() + b"\n")
            except Exception:
                pass
            conn.close()
    except SystemExit:
        pass
    finally:
        server.close()

def uds_request(size):
    start = time.perf_counter_ns()
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.connect(SOCKET_PATH)
    sock.sendall(json.dumps({"size": size}).encode())
    data = b""
    while True:
        chunk = sock.recv(65536)
        if not chunk:
            break
        data += chunk
        if b"\n" in chunk:
            break
    sock.close()
    return time.perf_counter_ns() - start

# --- SHM Setup ---
def make_shm():
    try:
        shm = shared_memory.SharedMemory(name=SHM_NAME, create=True, size=SLOT_SIZE + 64)
        shm.buf[:] = b"\x00" * (SLOT_SIZE + 64)
        return shm
    except FileExistsError:
        shm = shared_memory.SharedMemory(name=SHM_NAME)
        return shm

def shm_write(shm, data):
    buf = shm.buf
    seq = struct.unpack_from("I", buf, 0)[0]
    struct.pack_into("I", buf, 0, seq + 1)
    payload = struct.pack("I", len(data)) + data
    payload = payload.ljust(SLOT_SIZE, b"\x00")
    buf[64:64 + SLOT_SIZE] = payload
    struct.pack_into("I", buf, 0, seq + 2)

def shm_read(shm):
    buf = shm.buf
    while True:
        seq1 = struct.unpack_from("I", buf, 0)[0]
        if seq1 & 1:
            continue
        payload = bytes(buf[64:64 + SLOT_SIZE])
        seq2 = struct.unpack_from("I", buf, 0)[0]
        if seq1 == seq2 and not (seq2 & 1):
            data_len = struct.unpack_from("I", payload, 0)[0]
            return payload[4:4 + data_len]

def shm_measure(shm, size):
    data = b"x" * size
    shm_write(shm, data)
    start = time.perf_counter_ns()
    _ = shm_read(shm)
    return time.perf_counter_ns() - start

# --- Main ---
if __name__ == "__main__":
    sizes = [100, 1_000, 10_000, 100_000, 500_000]

    # Start UDS server
    uds_proc = multiprocessing.Process(target=uds_server, daemon=True)
    uds_proc.start()
    time.sleep(0.2)

    # Start SHM
    shm = make_shm()
    shm_write(shm, b"warmup")

    # Warm
    uds_request(100)
    shm_read(shm)

    print(f"{'Size':>12s} | {'UDS avg':>12s} | {'SHM avg':>12s} | {'Ratio':>8s}")
    print("-" * 50)

    for size in sizes:
        uds_times = [uds_request(size) for _ in range(50)]
        shm_times = [shm_measure(shm, size) for _ in range(50)]
        u_avg = sum(uds_times) / len(uds_times) / 1000
        s_avg = sum(shm_times) / len(shm_times) / 1000
        ratio = u_avg / s_avg if s_avg > 0 else float('inf')
        print(f"{size:>10,} B | {u_avg:>9.1f} µs | {s_avg:>9.1f} µs | {ratio:>7.1f}×")

    shm.close()
    try:
        shm.unlink()
    except:
        pass
    uds_proc.terminate()
    os.unlink(SOCKET_PATH) if os.path.exists(SOCKET_PATH) else None
