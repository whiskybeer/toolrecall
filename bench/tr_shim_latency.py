#!/usr/bin/env python3
"""T1 — shim read latency, p50/p95 (plan W2).

Measures the three paths the shim can take for the same file, over N reads:

  direct  — the OS ``open()`` the shim replaces (page cache warm)
  shim    — ``shim._shim_open`` served by a daemon cache hit (UDS round trip)
  local   — ``shim._shim_open`` served from the in-process LRU (W2 T3+)

Why this exists: the current hit path pays a daemon round trip on EVERY read
(~2.46 ms measured 2026-11-07). That is slower than not caching at all, so a
cache hit only wins if it is cheaper than the open it replaces. W2 target:
local hits p50 <= 0.2 ms.

Honesty rules baked in:
  - ``direct`` uses ``shim._original_open``, so a box with the .pth shim
    installed still measures a real open instead of recursing into the shim.
  - ``shim`` verifies the daemon actually answered with a cache HIT; a
    daemon-down run silently falls back to a real open and would otherwise
    produce a flattering, wrong number.
  - ``local`` is reported as UNVERIFIED when no local path exists yet; it
    never borrows the shim number.

Run against an ISOLATED daemon so live savings counters are untouched:

    TOOLRECALL_TRANSPORT=/tmp/trlat.sock \\
    TOOLRECALL_CACHE_DB=/tmp/trlat.db \\
    TOOLRECALL_FORWARD_PORT=18569 \\
    python3 bench/tr_shim_latency.py -n 300 --size 15000
"""

from __future__ import annotations

import argparse
import os
import statistics
import sys
import tempfile
import time

# Stubbing ON would replace the content on the 2nd+ read and time the stub
# path instead of the hit path. This bench measures content serving only.
os.environ.setdefault("TOOLRECALL_SHIM_STUB", "0")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import toolrecall.shim as shim_mod  # noqa: E402


def pct(values: list[float], q: float) -> float:
    """Linear-interpolated percentile of an already-sorted list."""
    if not values:
        return float("nan")
    if len(values) == 1:
        return values[0]
    pos = (len(values) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(values) - 1)
    return values[lo] + (values[hi] - values[lo]) * (pos - lo)


def summarize(name: str, samples_ms: list[float], reads: int, note: str = "") -> dict:
    s = sorted(samples_ms)
    row = {
        "mode": name,
        "n": reads,
        "p50": pct(s, 0.50),
        "p95": pct(s, 0.95),
        "mean": statistics.fmean(s),
        "max": s[-1],
        "note": note,
    }
    return row


def make_fixture(size: int) -> str:
    """A text file of ~``size`` bytes with a .py suffix (rb-servable)."""
    d = tempfile.mkdtemp(prefix="tr-lat-")
    path = os.path.join(d, "fixture.py")
    line = "VALUE = 12345  # latency fixture line\n"
    body = line * max(1, size // len(line))
    with open(path, "w", encoding="utf-8") as f:
        f.write(body)
    return path


def time_calls(fn, n: int) -> list[float]:
    out = []
    for _ in range(n):
        t0 = time.perf_counter()
        fn()
        out.append((time.perf_counter() - t0) * 1000.0)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("-n", "--reads", type=int, default=300)
    ap.add_argument("--size", type=int, default=15000)
    args = ap.parse_args()
    n = args.reads

    path = make_fixture(args.size)
    size = os.path.getsize(path)
    print(f"fixture: {path} ({size} bytes), {n} reads per mode")
    print(f"transport: {os.environ.get('TOOLRECALL_TRANSPORT', '<default socket>')}")
    print(f"cache db : {os.environ.get('TOOLRECALL_CACHE_DB', '<default>')}")

    def disk_bytes() -> bytes:
        # _original_open: a box with the .pth shim installed has
        # builtins.open patched, and that would compare cache to itself.
        f = shim_mod._original_open(path, "rb")
        try:
            return f.read()
        finally:
            f.close()

    def read_once() -> bytes:
        f = shim_mod._shim_open(path, "rb")
        try:
            return f.read()
        finally:
            f.close()

    # ── direct: the open() the shim replaces ──────────────────────────────
    def direct_read():
        f = shim_mod._original_open(path, "rb")
        try:
            f.read()
        finally:
            f.close()

    for _ in range(20):  # page-cache warm
        direct_read()
    direct = time_calls(direct_read, n)

    # ── shim: daemon cache hit ────────────────────────────────────────────
    tr = shim_mod._get_tr()
    shim_rows = []
    local_row = None
    if not tr:
        print("\nshim: client unavailable (toolrecall not importable) — skipped")
    else:
        hit_flags: list[bool] = []
        real_read = tr["read"]

        def counting_read(p, **kwargs):
            r = real_read(p, **kwargs)
            hit_flags.append(bool(isinstance(r, dict) and r.get("cached")))
            return r

        # Measure the daemon path exactly as it was before T3: with the local
        # fast path ON, a repeated read never reaches the daemon, so this
        # column would silently measure the fast path instead.
        os.environ["TOOLRECALL_SHIM_LOCAL"] = "0"
        tr["read"] = counting_read  # type: ignore[assignment]
        try:
            # Warm + prove hits: if the daemon is down every read falls back
            # to a real open and the timing would be a lie.
            exact = 0
            for _ in range(10):
                f = shim_mod._shim_open(path, "rb")
                try:
                    exact += f.read() == disk_bytes()
                finally:
                    f.close()
            hit_flags.clear()

            def shim_read():
                f = shim_mod._shim_open(path, "rb")
                try:
                    f.read()
                finally:
                    f.close()

            shim_samples = time_calls(shim_read, n)
            hits = sum(1 for h in hit_flags if h)
            if hits != n:
                print(
                    f"\nshim: ABORT — {n - hits}/{n} reads were not cache hits "
                    "(daemon down or cache miss); no number reported."
                )
                shim_samples = []
            elif exact != 10:
                print("\nshim: ABORT — served bytes differ from disk; no number reported.")
                shim_samples = []
            else:
                shim_rows.append(summarize("shim (daemon hit, local off)", shim_samples, n))

            # ── local: in-process LRU (T3+), optional ────────────────────
            # A local hit must satisfy BOTH: the client was not called (it
            # raises here) and the fixture itself was never opened. Byte
            # comparison alone is NOT proof — on a miss the shim falls through
            # to a real open() and returns identical bytes, which would report
            # a flattering fake "local" number (caught in the first T1 run).
            if shim_samples:
                # Re-enable the fast path and start cold, then warm exactly one
                # entry with the client still live. Flush the warm-up's counter
                # batch first so its IPC does not land inside the timed loop.
                os.environ.pop("TOOLRECALL_SHIM_LOCAL", None)
                shim_mod.local_cache_reset()
                read_once()  # miss -> daemon hit -> entry stored
                read_once()  # served from the entry already
                shim_mod._local_flush_pending()
                expected = disk_bytes()
                ipc_calls = []
                sends: list[dict] = []
                real_send = tr.get("send")

                def counting_send(payload, **kw):
                    sends.append(payload)
                    return real_send(payload, **kw) if real_send else {}

                def exploding_read(p, **kwargs):
                    ipc_calls.append(p)
                    raise RuntimeError("IPC attempted for a warm local hit")

                real_open = shim_mod._original_open
                disk_reads = []

                def guarded_open(p, *a, **k):
                    if os.path.abspath(os.fspath(p)) == os.path.abspath(path):
                        disk_reads.append(p)
                        raise RuntimeError("disk read on the local-hit path")
                    return real_open(p, *a, **k)

                tr["read"] = exploding_read  # type: ignore[assignment]
                if real_send is not None:
                    tr["send"] = counting_send  # type: ignore[assignment]
                shim_mod._original_open = guarded_open
                local_samples = []
                served = 0
                try:
                    for _ in range(n):
                        try:
                            t0 = time.perf_counter()
                            f = shim_mod._shim_open(path, "rb")
                            try:
                                body = f.read()
                            finally:
                                f.close()
                            dt = (time.perf_counter() - t0) * 1000.0
                        except Exception:
                            break
                        if body == expected:
                            served += 1
                            local_samples.append(dt)
                finally:
                    tr["read"] = real_read
                    if real_send is not None:
                        tr["send"] = real_send  # type: ignore[assignment]
                    shim_mod._original_open = real_open

                if served == n and not disk_reads and not ipc_calls:
                    batches = len(sends)
                    note = (
                        f"{batches} counter batch(es) in the timed loop "
                        f"(~1 per {max(1, n // batches)} hits)"
                        if batches
                        else "no counter batches"
                    )
                    local_row = summarize("local (in-process LRU)", local_samples, n, note)
                else:
                    why = []
                    if served != n:
                        why.append(f"{served}/{n} reads served")
                    if ipc_calls:
                        why.append(f"{len(ipc_calls)} IPC calls")
                    if disk_reads:
                        why.append(f"{len(disk_reads)} fixture disk reads")
                    local_row = {
                        "mode": "local (in-process LRU)",
                        "n": 0,
                        "p50": float("nan"),
                        "p95": float("nan"),
                        "mean": float("nan"),
                        "max": float("nan"),
                        "note": "UNVERIFIED — " + ", ".join(why),
                    }
        finally:
            tr["read"] = real_read
            os.environ.pop("TOOLRECALL_SHIM_LOCAL", None)

    rows = [summarize("direct (OS open)", direct, n)] + shim_rows
    if local_row:
        rows.append(local_row)

    print()
    hdr = f"{'mode':34} {'p50 ms':>9} {'p95 ms':>9} {'mean ms':>9} {'max ms':>9}  note"
    print(hdr)
    print("-" * len(hdr))
    for r in rows:
        print(
            f"{r['mode']:34} {r['p50']:9.3f} {r['p95']:9.3f} "
            f"{r['mean']:9.3f} {r['max']:9.3f}  {r['note']}"
        )

    # T6 gate: p50 target, plus the speedup it buys over the daemon path.
    target = 0.2
    if local_row and local_row["n"]:
        verdict = "PASS" if local_row["p50"] <= target else "FAIL"
        speedup = (shim_rows[0]["p50"] / local_row["p50"]) if shim_rows else float("nan")
        print(
            f"\nT6: local p50 <= {target} ms -> {verdict} "
            f"({local_row['p50']:.3f} ms; {speedup:.1f}x faster than a daemon hit)"
        )
        return 0 if verdict == "PASS" else 1
    print(f"\nT6: local p50 <= {target} ms -> UNVERIFIED (local path not measured)")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
