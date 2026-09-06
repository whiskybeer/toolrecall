"""Regression tests for the escaped-daemon env leak.

Root cause (2026-09): the production daemon was auto-started by
``_ensure_daemon()`` from inside a pytest run. The spawned process inherited
the test suite's hermetic env (``TOOLRECALL_KNOWLEDGE_DB`` /
``TOOLRECALL_CACHE_DB`` pointing at per-test temp paths), survived the test
via ``start_new_session=True``, and won the production socket. Result: the
live daemon served a deleted pytest tmp DB and every ``docs_search`` returned
"No knowledge database found" while ``knowledge.db`` (3 GB, 12k pages) sat
untouched on disk.

These tests pin both halves of the fix:

1. ``_clean_daemon_env()`` strips path-overriding env vars so an
   auto-started daemon resolves DB paths from config, not the caller's
   (possibly test-poisoned) environment.
2. ``run_daemon`` warns at startup when an env override points at a
   missing file — the silent-failure mode becomes visible.
"""

import io
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from toolrecall.cli import _clean_daemon_env


def _capture_stderr(fn, *args, **kwargs):
    """Run fn() capturing real stderr.

    The release gate runs pytest with ``-p no:capture``, which disables the
    capsys fixture. Patch sys.stderr directly instead.
    """
    buf = io.StringIO()
    orig = sys.stderr
    sys.stderr = buf
    try:
        fn(*args, **kwargs)
    finally:
        sys.stderr = orig
    return buf.getvalue()


class TestCleanDaemonEnv:
    """_clean_daemon_env() must strip path overrides from the spawn env."""

    def test_strips_path_overrides(self):
        poisoned = {
            "TOOLRECALL_CACHE_DB": "/tmp/pytest-of-hermes/pytest-104/hermetic.db",
            "TOOLRECALL_KNOWLEDGE_DB": "/tmp/pytest-of-hermes/pytest-104/hermetic.db",
            "TOOLRECALL_UDS_PATH": "/tmp/pytest-of-hermes/pytest-104/t.sock",
            "TOOLRECALL_CONFIG": "/tmp/pytest-of-hermes/pytest-104/cfg.toml",
            "PATH": os.environ.get("PATH", "/usr/bin"),
        }
        with patch.dict(os.environ, poisoned, clear=True):
            env = _clean_daemon_env()
        for var in (
            "TOOLRECALL_CACHE_DB",
            "TOOLRECALL_KNOWLEDGE_DB",
            "TOOLRECALL_UDS_PATH",
            "TOOLRECALL_CONFIG",
        ):
            assert var not in env, f"{var} leaked into daemon spawn env"
        # Non-path vars survive
        assert env["PATH"] == poisoned["PATH"]

    def test_clean_env_untouched(self):
        with patch.dict(os.environ, {"PATH": os.environ.get("PATH", "")}, clear=True):
            env = _clean_daemon_env()
        assert "TOOLRECALL_CACHE_DB" not in env
        assert "TOOLRECALL_KNOWLEDGE_DB" not in env

    def test_spawns_without_test_overrides_end_to_end(self):
        """A subprocess built from _clean_daemon_env() must not see the vars,
        even when the parent (simulated pytest process) has them set."""
        fake_db = "/tmp/toolrecall-leak-test-should-not-inherit.db"
        parent_env = dict(os.environ)
        parent_env["TOOLRECALL_KNOWLEDGE_DB"] = fake_db
        parent_env["TOOLRECALL_CACHE_DB"] = fake_db

        with patch.dict(os.environ, parent_env, clear=True):
            code = (
                "import os, json; print(json.dumps("
                "{k: v for k, v in os.environ.items() if k.startswith('TOOLRECALL')}))"
            )
            result = subprocess.run(
                [sys.executable, "-c", code],
                env=_clean_daemon_env(),
                capture_output=True,
                text=True,
                timeout=30,
            )
        inherited = eval(result.stdout.strip() or "{}")
        assert "TOOLRECALL_KNOWLEDGE_DB" not in inherited, (
            f"child inherited test env override: {inherited}"
        )
        assert "TOOLRECALL_CACHE_DB" not in inherited


class TestDaemonStartupWarning:
    """run_daemon must warn when env overrides point at missing files."""

    def test_warns_on_missing_override_file(self):
        from toolrecall.daemon import _warn_on_env_path_overrides

        def run():
            with patch.dict(
                os.environ,
                {"TOOLRECALL_KNOWLEDGE_DB": "/nonexistent/toolrecall-dead.db"},
                clear=False,
            ):
                _warn_on_env_path_overrides()

        err = _capture_stderr(run)
        assert "TOOLRECALL_KNOWLEDGE_DB" in err
        assert "WARNING" in err

    def test_silent_when_override_exists(self, tmp_path):
        from toolrecall.daemon import _warn_on_env_path_overrides

        real_file = tmp_path / "real.db"
        real_file.touch()

        def run():
            with patch.dict(os.environ, {"TOOLRECALL_KNOWLEDGE_DB": str(real_file)}, clear=False):
                _warn_on_env_path_overrides()

        err = _capture_stderr(run)
        assert "TOOLRECALL_KNOWLEDGE_DB" not in err

    def test_silent_when_unset(self):
        from toolrecall.daemon import _warn_on_env_path_overrides

        clean = {k: v for k, v in os.environ.items() if not k.startswith("TOOLRECALL")}

        def run():
            with patch.dict(os.environ, clean, clear=True):
                _warn_on_env_path_overrides()

        err = _capture_stderr(run)
        assert "WARNING" not in err
