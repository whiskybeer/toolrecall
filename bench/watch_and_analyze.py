#!/usr/bin/env python3
"""Watch the benchmark process, run analysis when done."""
import subprocess
import time
import os
import sys

BENCH_PID = 2409908
POLL_SEC = 120  # every 2 minutes

def pid_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False

print(f"Watching benchmark PID {BENCH_PID}...", flush=True)
while pid_alive(BENCH_PID):
    time.sleep(POLL_SEC)

print("Benchmark process exited. Running analyze...", flush=True)

time.sleep(5)

os.chdir("/home/hermes/toolrecall")
result = subprocess.run(
    ["/tmp/bench-env/bin/python3", "bench/analyze.py",
     "--db", os.path.expanduser("~/.toolrecall/cache.db")],
    capture_output=True, text=True, timeout=120
)
print(result.stdout)
if result.stderr:
    print("STDERR:", result.stderr[:2000], flush=True)

for f in ["benchmark_stats.txt", "BENCHMARK_REPORT.md",
          "fig1_context_growth.png", "fig2_ratio.png", "fig3_warmup.png"]:
    path = f"/home/hermes/toolrecall/{f}"
    if os.path.exists(path):
        print(f"✅ {f} ({os.path.getsize(path)} bytes)", flush=True)
    else:
        print(f"❌ {f} not found", flush=True)

print("\nDONE", flush=True)