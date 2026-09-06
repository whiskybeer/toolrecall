"""Central logging for ToolRecall — stdlib only, opt-out, zero sensitive data.

Design contract (v0.8.20):

* **Default ON.** One ``RotatingFileHandler`` attached to the ``toolrecall``
  root logger (``propagate=False``) on daemon/proxy/CLI start. The host
  agent's logging is never touched: no ``basicConfig``, no root handlers,
  no ``logging.lastResort`` involvement. A missing file, unwritable dir, or
  any handler failure degrades to no logging — never an error surfaced to
  the user.
* **7-day retention by default**, overridable via ``TOOLRECALL_LOG_MAX_DAYS``
  (or ``[log] max_days`` in config).
* **Stdlib only** (``logging``, ``logging.handlers``, ``pathlib``) — keeps
  the zero-runtime-dependencies identity (dependencies=[]).
* **Sensitive data is a no-go** (user directive, 2026-09-04): call
  :func:`redact` / :func:`redact_text` before logging anything request-
  shaped. They strip :data:`SENSITIVE_KEYS` fields and scrub secret-shaped
  values from free text. This module never logs payloads — only paths
  (leaf names), hashes, sizes, durations, and outcomes.
* **Hermetic-test safe**: ``TOOLRECALL_LOG_FILE`` / ``TOOLRECALL_LOG_LEVEL``
  resolve at setup time only, and :func:`reset_logging` restores the
  pristine pre-import state for tests.
"""

from __future__ import annotations

import logging
import os
import re
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

__all__ = [
    "SENSITIVE_KEYS",
    "get_logger",
    "redact",
    "redact_text",
    "setup_from_config",
    "setup_logging",
    "reset_logging",
]

# ── Sensitive-data policy ────────────────────────────────────────────
# Field names whose values are never logged, in any form. Used by redact()
# below; kept as data so tests and future handlers can reuse the policy.
SENSITIVE_KEYS = frozenset(
    {
        "token",
        "tokens",
        "password",
        "passwd",
        "pwd",
        "secret",
        "key",
        "keys",
        "api_key",
        "apikey",
        "authorization",
        "credential",
        "credentials",
        # Payload-shaped fields: even non-secret values are off-limits —
        # payloads can embed secrets in unexpected places (file contents,
        # terminal output, tool arguments).
        "payload",
        "arguments",
        "args",
        "content",
        "body",
        "data",
        "response",
        "response_body",
        "result",
        "value",
        "values",
        "cmd_text",
        "text",
        "message",
        "request",
        "request_body",
        "stdout",
        "stderr",
    }
)

# Secret-shaped values in free text (redact_text second net). Conservative:
# only high-entropy assignment-style leaks (KEY=..., token=...), never paths,
# hashes, or durations, so redaction cannot mangle debug info.
_SENSITIVE_RE = re.compile(
    r"(?i)\b((?:api[_-]?key|secret|token|password|passwd|authorization|"
    r"bearer|credential)[\w-]*\s*[=:]\s*)(\"[^\"]*\"|'[^']*'|\S+)"
)

_REDACTED = "[REDACTED]"


def redact_text(text: str) -> str:
    """Scrub secret-shaped ``key=value`` / ``key: value`` leaks from text."""
    if not isinstance(text, str):
        return text
    return _SENSITIVE_RE.sub(r"\1" + _REDACTED, text)


def redact(obj: object, _depth: int = 0) -> object:
    """Return a payload-safe copy of *obj* for logging.

    Dicts: sensitive keys (case-insensitive, per :data:`SENSITIVE_KEYS`)
    become ``"[REDACTED]"``; nested values are recursed. Lists/tuples map
    element-wise. Scalars pass through unchanged except that long strings
    (>512 chars) collapse to a head+length marker. Non-dict/list scalars
    never contain payloads, so they are logged as-is.
    """
    if _depth > 6:
        return "…"
    if isinstance(obj, dict):
        out: dict = {}
        for k, v in obj.items():
            ks = str(k)
            if ks.lower() in SENSITIVE_KEYS or _SENSITIVE_RE.fullmatch(ks):
                out[ks] = _REDACTED
            else:
                out[ks] = redact(v, _depth + 1)
        return out
    if isinstance(obj, (list, tuple)):
        return [redact(v, _depth + 1) for v in obj]
    if isinstance(obj, str) and len(obj) > 512:
        return f"<str len={len(obj)} head={obj[:80]!r}>"
    return obj


class _RedactingFilter(logging.Filter):
    """Last-line filter: scrub secret-shaped text from every record."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact_text(record.getMessage() if isinstance(record.msg, str) else record.msg)
        record.args = None  # args already merged; avoids double-format issues
        return True


# Singleton setup state — idempotent, test-resettable.
_handler: TimedRotatingFileHandler | None = None
_log_path: Path | None = None


def setup_logging(
    log_file: str | None = None,
    level: str | None = None,
    max_days: int | None = None,
) -> logging.Handler | None:
    """Attach the daily-rotating file handler to the ``toolrecall`` logger tree.

    Idempotent: re-calling with the same config is a no-op. Resolution order
    matches the config contract (env > config file > default):
    ``TOOLRECALL_LOG_FILE``/``TOOLRECALL_LOG_LEVEL``/``TOOLRECALL_LOG_MAX_DAYS``
    env vars win over the *log_file/level/max_days* arguments (which come
    from ``[log]`` in toolrecall.toml via :func:`setup_from_config`).
    Retention defaults to 7 days. Returns the handler, or ``None`` when
    logging could not be initialized (fail-soft).
    """
    global _handler, _log_path
    if _handler is not None:
        return _handler

    log_file = (
        os.environ.get("TOOLRECALL_LOG_FILE")
        or log_file
        or str(Path.home() / ".toolrecall" / "logs" / "toolrecall.log")
    )
    level = (os.environ.get("TOOLRECALL_LOG_LEVEL") or level or "INFO").upper()
    max_days = int(os.environ.get("TOOLRECALL_LOG_MAX_DAYS") or max_days or 7)

    root = logging.getLogger("toolrecall")
    root.setLevel(getattr(logging, level, logging.INFO))
    root.propagate = False  # never touch the host agent's handlers

    log_path = Path(log_file).expanduser()
    handler: logging.Handler
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        # Time-based rotation is the retention contract: one file per day,
        # ``max_days`` files kept (default 7). Per-day volume is bounded by
        # design — one INFO line per daemon request, payload-free.
        handler = TimedRotatingFileHandler(
            log_path, when="midnight", backupCount=max_days, encoding="utf-8"
        )
    except OSError:
        # Fail-soft: unwritable path (e.g. hermetic test cwd) → no logging.
        return None

    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
        )
    )
    handler.addFilter(_RedactingFilter())
    root.addHandler(handler)

    _handler = handler
    _log_path = log_path
    return handler


def get_logger(name: str) -> logging.Logger:
    """Named child logger (``toolrecall.<x>``) with fail-soft setup."""
    setup_logging()
    return logging.getLogger(f"toolrecall.{name}")


def setup_from_config(cfg) -> logging.Handler | None:
    """Setup from a loaded ``Config`` — env vars still win (config contract)."""
    try:
        return setup_logging(
            log_file=cfg.get("log", "file"),
            level=cfg.get("log", "level"),
            max_days=cfg.get("log", "max_days"),
        )
    except Exception:
        return setup_logging()  # malformed config → env/defaults, never crash


def reset_logging() -> None:
    """Remove the handler and restore pristine state (tests only)."""
    global _handler, _log_path
    if _handler is not None:
        root = logging.getLogger("toolrecall")
        root.removeHandler(_handler)
        root.setLevel(logging.NOTSET)
        root.propagate = True
        _handler.close()
        _handler = None
        _log_path = None
