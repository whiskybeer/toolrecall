# Copyright (c) 2026 Robin Schultka
# SPDX-License-Identifier: MIT
# Source: https://github.com/whiskybeer/toolrecall
"""Tests: shim unchanged-file stub (Lever 1, shim surface).

The state machine itself is pinned in test_stub_core.py (shared with the
bridge). This file pins the SHIM wiring: process-global instance, TTL
resolution (env > [shim].stub_ttl config > 300s default), the disable env,
invalidate-reset, and the end-to-end native-open path.
"""

import os
import tempfile
import unittest

import toolrecall.shim as shim_mod
import toolrecall.shim_stub as stub_mod


class _InstanceGuard:
    """Save/restore the process-global stub instance + its env knobs."""

    def __enter__(self):
        self._inst = stub_mod._instance
        self._env = {
            k: os.environ.get(k)
            for k in ("TOOLRECALL_SHIM_STUB", "TOOLRECALL_SHIM_STUB_TTL", "TOOLRECALL_CONFIG")
        }
        stub_mod._instance = None
        for k in self._env:
            os.environ.pop(k, None)
        return self

    def __exit__(self, *exc):
        stub_mod._instance = self._inst
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        return False


class TestShimStubWiring(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self._guard = _InstanceGuard().__enter__()

    def tearDown(self):
        self._guard.__exit__()
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)

    BODY = "x = 1\n" + "# pad " + "y" * 4000 + "\n"  # >800 chars

    def _probe(self, body=None):
        body = body or self.BODY
        p = os.path.join(self.tmpdir, "probe.py")
        with open(p, "w") as f:
            f.write(body)
        return p

    def test_default_policy_matches_shim_contract(self):
        # deterministic: pin a config without stub_ttl → documented default
        cfg = os.path.join(self.tmpdir, "tr.toml")
        with open(cfg, "w") as f:
            f.write("[shim]\nexclude_prefixes = []\n")
        os.environ["TOOLRECALL_CONFIG"] = cfg
        inst = stub_mod.instance()
        self.assertEqual(inst.max_consecutive, 2)  # compaction armor
        self.assertEqual(inst.min_chars, 800)  # request-dedup floor
        self.assertEqual(inst.ttl_s, 300.0)  # documented default
        note = inst._note_fn(0)
        self.assertIn("cache_refresh_file", note)  # shim note, not bypass_cache

    def test_disable_env_yields_permanent_noop(self):
        os.environ["TOOLRECALL_SHIM_STUB"] = "0"
        p = self._probe()
        self.assertIsNone(stub_mod.maybe_stub(p, self.BODY, True, True))
        self.assertIsNone(stub_mod.maybe_stub(p, self.BODY, True, True))
        # The call-time check dominates — the instance's configured TTL is
        # irrelevant while the kill switch is set (state also never arms:
        # the core is never reached, so a later re-enable starts fresh).

    def test_env_ttl_wins_over_config(self):
        os.environ["TOOLRECALL_SHIM_STUB_TTL"] = "77"
        self.assertEqual(stub_mod.instance().ttl_s, 77.0)

    def test_config_ttl_used_when_env_unset(self):
        cfg = os.path.join(self.tmpdir, "tr.toml")
        with open(cfg, "w") as f:
            f.write("[shim]\nstub_ttl = 123\n")
        os.environ["TOOLRECALL_CONFIG"] = cfg
        self.assertEqual(stub_mod.instance().ttl_s, 123.0)

    def test_reentrant_config_load_cannot_deadlock(self):
        # Regression for the full-suite freeze (2026-09-18, caught by pytest
        # faulthandler): when builtins.open is shim-patched (an earlier test
        # in the same process patched it, or the harness runs shimmed), the
        # lazy instance()'s load_config() → open(config) re-entered
        # maybe_stub → instance() and deadlocked on the non-reentrant lock.
        # The per-thread re-entrancy guard in instance()/maybe_stub must
        # short-circuit any re-entrant open to "serve content, touch no
        # state" — including direct instance() calls (reset_path, tests).
        with _InstanceGuard():
            cfg = os.path.join(self.tmpdir, "tr.toml")
            with open(cfg, "w") as f:
                f.write("[shim]\nstub_ttl = 111\n")
            os.environ["TOOLRECALL_CONFIG"] = cfg

            real_open = open  # capture BEFORE patching (shim does the same)

            def _open_with_recursion(path, *a, **kw):
                # Simulate a shim-patched open() during config load: every
                # real open re-enters the module API (exactly what the
                # patched builtins.open does on the shim path).
                stub_mod.maybe_stub(path, "x" * 900, True, True)
                return real_open(path, *a, **kw)

            from unittest.mock import patch as mock_patch

            with mock_patch("builtins.open", _open_with_recursion):
                inst = stub_mod.instance()  # resolves TTL via load_config()

            self.assertEqual(inst.ttl_s, 111.0)
            # The re-entrant recursion probes served content, armed NO state
            # (FileStubSession state dict is `_session`).
            self.assertEqual(len(inst._session), 0)

            # And once built, the guard still short-circuits re-entrant
            # maybe_stub calls issued from INSIDE a stub window.
            p = self._probe()
            self.assertIsNone(stub_mod.maybe_stub(p, self.BODY, True, True))
            stub_mod._reentrant.active = True
            try:
                # re-entrant → serve, no state change
                self.assertIsNone(stub_mod.maybe_stub(p, self.BODY, True, True))
            finally:
                stub_mod._reentrant.active = False
            # Outer window closed: normal behavior resumes (2nd read stubs).
            self.assertIsNotNone(stub_mod.maybe_stub(p, self.BODY, True, True))
            stub_mod._reentrant.active = False

    def test_invalid_env_falls_back_to_config(self):
        cfg = os.path.join(self.tmpdir, "tr.toml")
        with open(cfg, "w") as f:
            f.write("[shim]\nstub_ttl = 123\n")
        os.environ["TOOLRECALL_CONFIG"] = cfg
        os.environ["TOOLRECALL_SHIM_STUB_TTL"] = "not-a-number"
        self.assertEqual(stub_mod.instance().ttl_s, 123.0)

    def test_stub_roundtrip_through_module_api(self):
        p = self._probe()
        self.assertIsNone(stub_mod.maybe_stub(p, self.BODY, True, True))
        note = stub_mod.maybe_stub(p, self.BODY, True, True)
        self.assertIsNotNone(note)
        stub_mod.reset_path(p)
        self.assertIsNone(stub_mod.maybe_stub(p, self.BODY, True, True))

    def test_invalidate_file_resets_stub_state(self):
        _os = os
        _os.environ["TOOLRECALL_CACHE_DB"] = self.tmpdir + "/t.db"
        from toolrecall.cache import invalidate_file

        p = self._probe()
        stub_mod.maybe_stub(p, self.BODY, True, True)
        invalidate_file(p)
        self.assertIsNone(stub_mod.maybe_stub(p, self.BODY, True, True))

    def test_shim_open_serves_stub_on_native_reread(self):
        # End-to-end through _shim_open with a stubbed hit: 2nd read returns
        # the note instead of full content (the wire-verified saving).
        # The mock read response carries source="agent_tool" — since the
        # 2026-09-18 live incident the shim stubs ONLY agent_tool reads
        # (a stub served to a gateway's own config re-read corrupted its
        # YAML parse). The agent-tool gate is pinned separately below.
        p = self._probe()
        shim_mod._SKIP_PREFIXES = []
        shim_mod._TR = {
            "read": lambda pp, **kw: {
                "cached": True,
                "content": self.BODY,
                "lossless": True,
                "source": "agent_tool",
            }
        }

        with unittest.mock.patch.object(shim_mod, "_process_is_agent", return_value=True):
            h1 = shim_mod._shim_open(p, "rb")
            d1 = h1.read()
            h1.close()
            h2 = shim_mod._shim_open(p, "rb")
            d2 = h2.read()
            h2.close()
        self.assertEqual(d1, self.BODY.encode())
        self.assertIn(b"unchanged", d2.lower())
        self.assertLess(len(d2), len(d1))

    def test_shim_open_never_stubs_infra_reads(self):
        # 2026-09-18 live incident: the gateway's own config re-read
        # (source="shim", library-path gate) got a stub note instead of its
        # YAML — parse failure + config-recovery backups. Infra reads must
        # always serve FULL cached content, no stub, regardless of TTL.
        p = self._probe()
        shim_mod._SKIP_PREFIXES = []
        shim_mod._TR = {
            "read": lambda pp, **kw: {
                "cached": True,
                "content": self.BODY,
                "lossless": True,
                "source": "shim",
            }
        }
        for _ in range(4):  # far past the 2-stub cap
            h = shim_mod._shim_open(p, "rb")
            d = h.read()
            h.close()
            self.assertEqual(d, self.BODY.encode())


if __name__ == "__main__":
    unittest.main()
