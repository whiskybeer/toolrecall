#!/usr/bin/env python3
"""
Demo: Shared Memory IPC via mmap + seqlock.

The server writes cache entries into a POSIX shared memory region.
The client reads them with ZERO syscalls — just a pointer dereference
via the mmap'd buffer.

Seqlock protocol (lock-free reads):
  - Writer: write_seq++, write data, write_seq++ (write_seq even = unlocked)
  - Reader: read_seq1, copy data, read_seq2; retry if read_seq1!=read_seq2 or odd
"""

import mmap, os, struct, time, sys, threading, multiprocessing
from multiprocessing import shared_memory

SHM_NAME = "toolrecall_demo_shm"
META_SIZE = 64
SLOT_COUNT = 64
SLOT_SIZE = 2048
TOTAL_SIZE = META_SIZE + (SLOT_COUNT * SLOT_SIZE)

OFF_SEQLOCK = 0
OFF_ACTIVE  = 8
OFF_KEYS    = 16

SLOT0_OFFSET = META_SIZE + (0 * SLOT_SIZE)


def make_shm():
    try:
        shm = shared_memory.SharedMemory(name=SHM_NAME, create=True, size=TOTAL_SIZE)
        shm.buf[:] = b"\x00" * TOTAL_SIZE
        return shm
    except FileExistsError:
        shm = shared_memory.SharedMemory(name=SHM_NAME)
        return shm


def shm_write_slot(shm, slot, data):
    offset = META_SIZE + (slot * SLOT_SIZE)
    buf = shm.buf

    seq = struct.unpack_from("I", buf, OFF_SEQLOCK)[0]
    struct.pack_into("I", buf, OFF_SEQLOCK, seq + 1)

    payload = struct.pack("I", len(data)) + data
    payload = payload.ljust(SLOT_SIZE, b"\x00")
    buf[offset:offset + SLOT_SIZE] = payload

    struct.pack_into("I", buf, OFF_SEQLOCK, seq + 2)
    struct.pack_into("Q", buf, OFF_ACTIVE, 1 << slot)


def shm_read_slot(shm, slot):
    buf = shm.buf
    offset = META_SIZE + (slot * SLOT_SIZE)

    while True:
        seq1 = struct.unpack_from("I", buf, OFF_SEQLOCK)[0]
        if seq1 & 1:
            continue

        active = struct.unpack_from("Q", buf, OFF_ACTIVE)[0]
        if not (active & (1 << slot)):
            return None

        # ★ ONE pointer dereference to the mmap'd buffer ★
        payload = bytes(buf[offset:offset + SLOT_SIZE])

        seq2 = struct.unpack_from("I", buf, OFF_SEQLOCK)[0]
        if seq1 == seq2 and not (seq2 & 1):
            data_len = struct.unpack_from("I", payload, 0)[0]
            return payload[4:4 + data_len]


def server_proc():
    shm = make_shm()
    try:
        cache_data = {
            0: b"Hello World! " * 100,
        }
        while True:
            for slot, data in cache_data.items():
                shm_write_slot(shm, slot, data)
            time.sleep(0.01)
    except KeyboardInterrupt:
        pass
    finally:
        try:
            shm.close()
            shm.unlink()
        except Exception:
            pass


def shm_read_latency(shm, slot, iterations=100):
    times = []
    for _ in range(iterations):
        start = time.perf_counter_ns()
        data = shm_read_slot(shm, slot)
        end = time.perf_counter_ns()
        times.append(end - start)
        if data is None:
            return times
    return times


if __name__ == "__main__":
    print(f"[SHM] Shared memory '{SHM_NAME}' — {TOTAL_SIZE} bytes")
    print(f"[SHM] Each slot: {SLOT_SIZE} bytes, {SLOT_COUNT} slots")

    proc = multiprocessing.Process(target=server_proc, daemon=True)
    proc.start()
    time.sleep(0.3)

    shm = make_shm()

    # Warm up
    shm_read_slot(shm, 0)

    print(f"\n[SHM] Reading slot 0 — content length: ~1300 bytes")
    t_small = shm_read_latency(shm, 0)
    avg = sum(t_small) / len(t_small)
    mn = min(t_small)
    mx = max(t_small)
    print(f"  slot 0 (small)  avg={avg/1000:8.1f}µs  min={mn/1000:6.1f}µs  max={mx/1000:8.1f}µs")
    print(f"  Nano seconds:   avg={avg:8.1f}ns  min={mn:6.1f}ns  max={mx:8.1f}ns")

    shm.close()
    try:
        shm.unlink()
    except Exception:
        pass
    proc.terminate()
    print(f"\n[SHM] Done.")
