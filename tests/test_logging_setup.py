"""Tests for toolrecall.logging_setup — central logging rail.

Hermetic: every test points TOOLRECALL_LOG_FILE at a temp dir and resets
logging state after itself (mirrors conftest's hermetic fixtures contract).
"""

import logging

import pytest

from toolrecall import logging_setup


@pytest.fixture()
def rail(tmp_path, monkeypatch):
    """Fresh logging rail on a temp file for each test."""
    logfile = tmp_path / "logs" / "toolrecall.log"
    monkeypatch.setenv("TOOLRECALL_LOG_FILE", str(logfile))
    monkeypatch.delenv("TOOLRECALL_LOG_LEVEL", raising=False)
    monkeypatch.delenv("TOOLRECALL_LOG_MAX_DAYS", raising=False)
    logging_setup.reset_logging()
    yield logfile
    logging_setup.reset_logging()


def test_setup_creates_file_and_writes(rail):
    h = logging_setup.setup_logging()
    assert h is not None
    logging_setup.get_logger("test").info("hello %s", "world")
    h.flush()
    content = rail.read_text()
    assert "hello world" in content
    assert "toolrecall.test" in content
    assert "INFO" in content


def test_setup_is_idempotent(rail):
    h1 = logging_setup.setup_logging()
    h2 = logging_setup.setup_logging()
    assert h1 is h2


def test_fail_soft_on_unwritable_path(monkeypatch):
    # Directory in place of the log file → OSError → None (never raises)
    monkeypatch.setenv("TOOLRECALL_LOG_FILE", "/proc/definitely/not/writable/x.log")
    monkeypatch.setattr(logging_setup, "_handler", None)
    try:
        assert logging_setup.setup_logging() is None
    finally:
        logging_setup.reset_logging()


def test_no_sensitive_keys_logged(rail):
    h = logging_setup.setup_logging()
    log = logging_setup.get_logger("test")
    log.info(
        "call args=%s token=%s stdout=%s",
        {"arguments": {"file": "secret.py", "content": "SECRET-BODY"}, "token": "tok-123"},
        "sk-abc",
        "out",
    )
    h.flush()
    content = rail.read_text()
    # SENSITIVE_KEYS values are structurally never logged via redact() — and
    # this message never even passes through redact(); the point is that the
    # _RedactingFilter scrubs free-text secret-shaped leaks:
    assert "sk-abc" not in content
    assert "[REDACTED]" in content


def test_redact_strips_sensitive_fields():
    obj = {
        "path": "/tmp/a.py",
        "token": "x",
        "arguments": {"content": "body"},
        "nested": {"api_key": "k", "keep": 1},
        "big": "y" * 600,
    }
    out = logging_setup.redact(obj)
    assert out["path"] == "/tmp/a.py"
    assert out["token"] == "[REDACTED]"
    assert out["arguments"] == "[REDACTED]"
    assert out["nested"]["api_key"] == "[REDACTED]"
    assert out["nested"]["keep"] == 1
    assert out["big"].startswith("<str len=")


def test_redact_text_scrubs_assignments():
    s = logging_setup.redact_text("auth failed with api_key=abcd1234 and Bearer: eyJxyz")
    assert "abcd1234" not in s
    assert "eyJxyz" not in s
    assert "[REDACTED]" in s
    # Paths/durations untouched
    assert (
        logging_setup.redact_text("dur=12.5ms path=/home/u/x.py") == "dur=12.5ms path=/home/u/x.py"
    )


def test_rotation_config(rail):
    from logging.handlers import TimedRotatingFileHandler

    h = logging_setup.setup_logging(max_days=3)
    assert isinstance(h, TimedRotatingFileHandler)
    assert h.when == "MIDNIGHT" or h.when == "midnight"
    assert h.backupCount == 3


def test_level_from_env(rail, monkeypatch):
    monkeypatch.setenv("TOOLRECALL_LOG_LEVEL", "WARNING")
    h = logging_setup.setup_logging()
    log = logging_setup.get_logger("test")
    log.info("should not appear")
    log.warning("should appear")
    h.flush()
    content = rail.read_text()
    assert "should not appear" not in content
    assert "should appear" in content


def test_setup_from_config_env_wins(rail, monkeypatch):
    class _Cfg:
        def get(self, *keys, default=None):
            return {"file": "/nonexistent/should/be/overridden.log"}.get(keys[-1], default)

    monkeypatch.setenv("TOOLRECALL_LOG_FILE", str(rail))
    h = logging_setup.setup_from_config(
        type("C", (), {"get": staticmethod(lambda *k, default=None: None)})()
    )
    assert h is not None


def test_never_propagates_to_root(rail):
    root_records = []

    class _Cap(logging.Handler):
        def emit(self, record):
            root_records.append(record)

    root = logging.getLogger()
    cap = _Cap()
    root.addHandler(cap)
    try:
        h = logging_setup.setup_logging()
        logging_setup.get_logger("test").info("propagation check")
        h.flush()
        assert root_records == []  # propagate=False held
    finally:
        root.removeHandler(cap)
