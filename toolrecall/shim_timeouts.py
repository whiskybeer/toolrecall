"""Shim read-path timeout accounting (client side).

The shim's ``cached_read`` runs inside the caller's ``open()``. When the daemon
was slow, transport's 5 s default was charged to every read (19 WebUI requests
stalled 5.5-5.8 s in 7 days, thread stack in ``recv_exact``). The read path now
has its own short budget plus a circuit breaker, and every timeout is recorded
here so ``toolrecall stats`` can show what the breaker is hiding.

Storage is an append-only JSONL under ``~/.toolrecall/``: the daemon is by
definition the thing that was unresponsive, so accounting must not require it.
One small ``O_APPEND`` write per timeout keeps this off the hot path — timeouts
are the exception, not the rule.
"""

from __future__ import annotations

import json
import logging
import os
import time

log = logging.getLogger("toolrecall.shim_timeouts")

_MAX_BYTES = 2 * 1024 * 1024  # rotate once past this; `stats` must stay cheap
_FILENAME = "shim_timeouts.jsonl"


def _log_path() -> str:
    return os.path.join(os.path.expanduser("~/.toolrecall"), _FILENAME)


def _rotate_if_large(path: str) -> None:
    try:
        if os.path.getsize(path) > _MAX_BYTES:
            os.replace(path, path + ".1")
    except OSError:
        log.debug("shim timeout log rotate failed", exc_info=True)


def record(path: str = "", reason: str = "timeout") -> None:
    """Append one timeout record. Never raises.

    A diagnostics counter must not be able to break a file read: every failure
    (missing dir, full disk, permission) is swallowed.
    """
    try:
        p = _log_path()
        os.makedirs(os.path.dirname(p), exist_ok=True)
        line = json.dumps({"ts": time.time(), "path": path[:512], "reason": reason}) + "\n"
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, line.encode("utf-8"))
        finally:
            os.close(fd)
        _rotate_if_large(p)
    except Exception:  # noqa: BLE001 — diagnostics must never break the caller
        log.debug("shim timeout record failed", exc_info=True)


def summary(window_s: float = 86400.0) -> dict:
    """Return ``{"total", "last_24h", "last"}`` over the append log.

    Tolerant by design: a truncated last line (killed writer) or a rewritten
    file must not turn a diagnostics read into an exception.
    """
    total = 0
    recent = 0
    last = None
    cutoff = time.time() - window_s
    try:
        with open(_log_path(), encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(rec, dict):
                    continue
                total += 1
                ts = rec.get("ts") or 0
                try:
                    ts = float(ts)
                except (TypeError, ValueError):
                    ts = 0.0
                if ts >= cutoff:
                    recent += 1
                if last is None or ts > last:
                    last = ts
    except OSError:
        log.debug("shim timeout summary unreadable", exc_info=True)
    return {"total": total, "last_24h": recent, "last": last}
