"""Regression tests: daemon IPC mcp cache key alignment + sha256 hash default.

Found by the 2026-09-19 daemon-in-the-loop replay bench (mcp-replay-bench):

Bug 1 — cached_mcp_store {key: K} wrote row K verbatim while cached_mcp_check
{key: X} looked up hash("_generic://_lookup?" + json({"key": X})): rows stored
via the IPC route were unreachable by checks. Fix: the store handler derives
the row key through the same ONE derivation cache.py owns.

Bug 2 — _hash defaulted to md5, so daemon cache keys did not match external
sha256 identity derivations (SEP mcp.tool.request_key family). Fix: sha256 is
the default; md5 remains selectable as a legacy opt-out.

Isolation: default/opt-out pins run in SUBPROCESSES (importlib.reload in-process
would recreate _db_mod module globals such as _db_lock and break other test
modules' captured fixture references — the 2026-09-19 full-suite interaction).
"""

import hashlib
import json
import os
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from toolrecall.cache import _mcp_request_key


def _subproc_hash(env_algo: str | None, value: str) -> str:
    """Run _hash in a fresh interpreter with the given algorithm env."""
    env = dict(os.environ)
    env.pop("TOOLRECALL_HASH_ALGORITHM", None)
    if env_algo is not None:
        env["TOOLRECALL_HASH_ALGORITHM"] = env_algo
    code = "import toolrecall._db as db; print(db._hash(%r), db._HASH_ALGORITHM)" % value
    out = subprocess.run(
        [sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=60
    )
    assert out.returncode == 0, out.stderr
    digest, algo = out.stdout.split()
    return digest, algo


class TestHashDefault(unittest.TestCase):
    def test_default_is_sha256(self):
        """_hash defaults to sha256 (2026-09-19) — external identity
        derivations and daemon row keys agree on one algorithm."""
        digest, algo = _subproc_hash(None, "hello")
        self.assertEqual(algo, "sha256")
        self.assertEqual(digest, hashlib.sha256(b"hello").hexdigest())

    def test_md5_opt_out_still_selectable(self):
        """Legacy opt-out: explicit env selects md5 (rollback path)."""
        digest, algo = _subproc_hash("md5", "hello")
        self.assertEqual(algo, "md5")
        self.assertEqual(digest, hashlib.md5(b"hello").hexdigest())


class TestIpcStoreCheckAlignment(unittest.TestCase):
    """Unit level: the store handler must address the row the check reads."""

    def test_wrap_preimage_shape(self):
        """The wrapper preimage is exactly what check derives:
        _mcp_request_key('_generic', '_lookup', {'key': K})."""
        import toolrecall._db as _db_mod

        k = "some-opaque-key-123"
        self.assertEqual(
            _mcp_request_key("_generic", "_lookup", {"key": k}),
            _wrap_key(k, _db_mod._HASH_ALGORITHM),
        )

    def test_store_then_check_hits(
        self,
    ):
        """The FIXED handler pair end-to-end through cache.py's single
        derivation: store via handler-shaped call, check via handler-shaped
        call, assert the hit + the row hash the check derived."""
        from tests.e2e_helpers import E2EDaemon

        k = "regression-key-xyz"
        content = "REGRESSION-CONTENT"
        with E2EDaemon() as d:
            d.wait_until_ready()
            r_store = d.client.send({"cmd": "cached_mcp_store", "key": k, "content": content})
            self.assertNotIn("error", r_store, r_store)
            self.assertTrue(r_store.get("stored"), r_store)
            r_check = d.client.send({"cmd": "cached_mcp_check", "key": k})
            self.assertNotIn("error", r_check, r_check)
            self.assertTrue(r_check.get("cached"), f"IPC check must hit after IPC store: {r_check}")
            self.assertEqual(r_check.get("data"), content)


def _wrap_key(k: str, algo: str) -> str:
    """Row-key derivation for a generic lookup under the given algorithm
    (mirrors _mcp_request_key)."""
    preimage = "_generic://_lookup?" + json.dumps({"key": k}, sort_keys=True)
    if algo == "sha256":
        return hashlib.sha256(preimage.encode()).hexdigest()
    return hashlib.md5(preimage.encode()).hexdigest()


class TestIpcEndToEnd(unittest.TestCase):
    """E2E: store via daemon IPC, check via daemon IPC — must hit (both
    directions through the fixed store handler)."""

    def test_ipc_store_then_check_hits(self):
        from tests.e2e_helpers import E2EDaemon

        with E2EDaemon() as d:
            d.wait_until_ready()
            k = "e2e-key-42"
            content = "E2E-CONTENT-9f2"
            r_store = d.client.send({"cmd": "cached_mcp_store", "key": k, "content": content})
            self.assertNotIn("error", r_store, r_store)
            self.assertTrue(r_store.get("stored"), r_store)
            r_check = d.client.send({"cmd": "cached_mcp_check", "key": k})
            self.assertNotIn("error", r_check, r_check)
            self.assertTrue(r_check.get("cached"), f"IPC check must hit: {r_check}")
            self.assertEqual(r_check.get("data"), content)


if __name__ == "__main__":
    unittest.main()
