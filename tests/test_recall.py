"""Tests for the recall tier.

Covers the opt-in config gate (default OFF) plus the config property.
deps-free, no network — mirrors the test_turso_optin gating pattern.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from toolrecall import _db  # noqa: E402
from toolrecall.config import load_config  # noqa: E402


class TestRecallConfigGate(unittest.TestCase):
    """The recall tier must be OFF unless explicitly enabled."""

    def setUp(self):
        self._orig = dict(os.environ)
        _db._cached_config = None

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._orig)
        _db._cached_config = None

    def test_recall_disabled_by_default(self):
        cfg = load_config()
        self.assertFalse(cfg.recall_enabled)

    def test_recall_enabled_env_coercion(self):
        from toolrecall.config import load_config

        for raw, expected in [
            ("true", True),
            ("1", True),
            ("yes", True),
            ("false", False),
            ("0", False),
            ("no", False),
            ("", False),
        ]:
            os.environ["TOOLRECALL_RECALL_ENABLED"] = raw
            cfg = load_config()
            self.assertEqual(cfg.recall_enabled, expected, f"raw={raw!r}")
        os.environ.pop("TOOLRECALL_RECALL_ENABLED")

    def test_recall_absent_key_defaults_off(self):
        # Even if some unrelated config is present, absent [recall] == off.
        cfg = load_config()
        self.assertFalse(cfg.get("recall", "enabled", default=False))


if __name__ == "__main__":
    unittest.main()
