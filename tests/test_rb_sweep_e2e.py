"""Byte-exactness E2E: rb serve must round-trip disk bytes exactly.

Runs the REAL stack hermetically — E2EDaemon (temp socket + temp DB) plus a
subprocess whose builtins.open is shim-patched — then checksums served bytes
against raw os.read bytes. Regression for the 2026-09-15 corruption class:
universal-newlines cache text re-encoded to bytes returned byte-different
content for CRLF files (raw acb24a12… vs served 1e19d262…).
"""

import os
import subprocess
import sys
import tempfile
import unittest

from tests.e2e_helpers import E2EDaemon

BODY = r"""
import hashlib, os, sys
TOP = sys.argv[1]
d = os.path.join(TOP, "files")
os.makedirs(d, exist_ok=True)

def raw(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        out = []
        while True:
            b = os.read(fd, 1 << 20)
            if not b:
                break
            out.append(b)
        return b"".join(out)
    finally:
        os.close(fd)

def shim_rb(path):
    with open(path, "rb") as fh:   # miss-through populates the cache
        fh.read()
    with open(path, "rb") as fh:   # hit path under test
        return fh.read()

cases = [
    ("utf8-lf.py", "def f():\n    return 'café ✓'\n".encode("utf-8")),
    ("bom.json", b"\xef\xbb\xbf" + b'{"k": 1}\n'),
    ("crlf.html", b"<html>\r\n<body>x</body>\r\n</html>\r\n"),
    ("latin1.py", b"# caf\xe9\nx = 1\n"),
    ("empty.md", b""),
    ("nul.txt", b"a\x00b\n"),
    ("blob.png", b"\x89PNG\r\n\x1a\n" + os.urandom(32)),
    ("Makefile", b"all:\n\techo hi\n"),
]
bad = 0
for name, blob in cases:
    p = os.path.join(d, name)
    with open(p, "wb") as f:
        f.write(blob)
    r, s = raw(p), shim_rb(p)
    if r != s:
        bad += 1
        print(f"MISMATCH {name}")
    else:
        print(f"OK       {name}  sha={hashlib.sha256(r).hexdigest()[:10]}")
print(f"rb-e2e: {len(cases)} files, {bad} mismatches")
sys.exit(1 if bad else 0)
"""


class TestRbServeByteExactness(unittest.TestCase):
    def test_rb_serve_round_trips_disk_bytes(self):
        d = tempfile.mkdtemp(prefix="tr-rb-e2e-")
        env = dict(os.environ)
        env["TOOLRECALL_SHIM_DISABLE"] = ""  # subprocess must get the shim
        env.pop("TOOLRECALL_SHIM_DISABLE", None)
        with E2EDaemon() as daemon:
            env["TOOLRECALL_TRANSPORT"] = daemon.socket_path
            env["TOOLRECALL_CACHE_DB"] = daemon.db_path
            r = subprocess.run(
                [sys.executable, "-c", BODY, d],
                env=env,
                capture_output=True,
                text=True,
                timeout=120,
            )
        self.assertEqual(
            r.returncode,
            0,
            f"rb sweep failed:\n{r.stdout}\nstderr:\n{r.stderr[-1500:]}",
        )
        self.assertIn("0 mismatches", r.stdout)


if __name__ == "__main__":
    unittest.main()
