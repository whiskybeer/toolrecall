"""ToolRecall daemon/cache healthcheck — generalized, self-contained.

Implements a stable, parameterized healthcheck that any operator can run
directly (`toolrecall healthcheck`) or wrap in a cron/systemd watchdog. It
reuses ToolRecall's **own** path helpers (`transport._default_socket_path`,
`daemon.PID_FILE`, `daemon._instance_lock_path`) so it always checks the same
files the daemon actually uses — no hardcoded per-machine paths.

Reported signals:
    procs        number of live `toolrecall daemon` processes (-1 unknown)
    pid_file     pid read from the daemon pid file ('' if absent)
    pid_alive    whether that pid maps to a live, non-zombie process
    lock_files   count of ~/.toolrecall/daemon-*.lck files
    socket_path  the resolved default UDS socket path
    socket       whether that socket exists
    shim_state   'active' | 'inactive' | 'unknown' (via `shim --status --all`)
    hit_rate     cache hit-rate (from `stats`), or None if stats unavailable

Command contract (`toolrecall healthcheck`):
    - prints a single human-readable status line on stdout
    - exits 0 when the daemon is healthy
    - exits 1 and prints a `note:` line + warning when something is abnormal
      (daemon down, stale pid, leaking lock files, missing socket)
    - `--json` prints one JSON object instead of the human line
    - `--clean-stale` first removes a *provably* dead `daemon.pid` and
      orphaned `daemon-*.lck` files (flock-verified), then reports health

Everything is configurable via the standard TOOLRECALL_* env vars and the
`TOOLRECALL_BIN` override (cron doesn't inherit ~/.local/bin PATH).
"""

from __future__ import annotations

import fnmatch
import logging
import os
import subprocess
import sys
import time
from typing import TypedDict

log = logging.getLogger(__name__)

# ── Defaults (overridable, matching the rest of the codebase) ──────────────
DEFAULT_CACHE_DIR = os.path.expanduser("~/.toolrecall")
EXIT_OK = 0
EXIT_WARN = 1
EXIT_UNKNOWN = 2

# Shim probe budget. The probe spawns one interpreter per venv: 3.2 s warm and
# 10.8-17.1 s under load for 3 venvs on this box. A 20 s budget made the daily
# verdict a load coin-flip, and the timeout was reported as "inactive" — telling
# an operator to reinstall a working shim (2026-11-10). This runs once a day, so
# the margin is free.
try:
    SHIM_PROBE_TIMEOUT_S = float(os.environ.get("TOOLRECALL_SHIM_PROBE_TIMEOUT_S", "60"))
except ValueError:  # a bad env value must not break a health check
    SHIM_PROBE_TIMEOUT_S = 60.0

SHIM_ACTIVE = "active"
SHIM_INACTIVE = "inactive"
SHIM_UNKNOWN = "unknown"


class ShimProbe(TypedDict):
    """The shim fields `_probe_shim_state` classifies.

    Its own TypedDict so `HealthInfo.update(probe)` type-checks: a bare ``dict``
    return would erase the key/value types at the one place they matter.

    ``shim_venvs`` / ``shim_bare`` / ``shim_bare_required`` carry the PER-VENV
    verdict. The aggregate ``shim_state`` says "a shim works somewhere", which
    is not the same question as "the interpreter that runs the agent is
    shimmed": on 2026-11-11 three venvs were discovered, one was shimmed, the
    agent runtime was missing the package, and the single-state report was
    green.
    """

    shim_state: str
    shim_reason: str
    shim_probe_s: float | None
    shim_venvs: list[dict]
    shim_bare: list[str]
    shim_bare_required: list[str]


class HealthInfo(TypedDict):
    procs: int
    pid_file: str
    pid_alive: bool
    lock_files: int
    socket_path: str
    socket: bool
    proxy: bool
    proxy_port: int
    shim_state: str
    shim_reason: str
    shim_probe_s: float | None
    shim_venvs: list[dict]
    shim_bare: list[str]
    shim_bare_required: list[str]
    hit_rate: float | None
    log_file: str
    log_errors_recent: int
    log_warnings_recent: int
    hermes_plugin: dict


# Honor the same env override the cron wrapper uses.
TOOLRECALL = os.environ.get("TOOLRECALL_BIN", "toolrecall")


def _validated_toolrecall_bin() -> list[str]:
    """TOOLRECALL_BIN is a documented override for cron environments that do
    not inherit ~/.local/bin PATH. It is env-tainted input reaching
    subprocess, so validate it strictly before use: allow absolute/relative
    paths to an existing executable file, or a bare name that resolves on
    PATH and points to a regular file. Fail closed otherwise."""
    raw = TOOLRECALL
    if not raw or any(c in raw for c in ("\x00", "\n")):
        return [TOOLRECALL]  # fall through; FileNotFoundError handler catches it
    if os.path.sep in raw:
        # Path form: must exist, be a regular file, and be executable by us.
        if os.path.isfile(raw) and os.access(raw, os.X_OK):
            return [raw]
        return [TOOLRECALL]
    # Bare name: must resolve on PATH to a regular executable file.
    for dir_ in os.environ.get("PATH", "").split(os.pathsep):
        if not dir_:
            continue
        candidate = os.path.join(dir_, raw)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return [raw]
    return [TOOLRECALL]


def _default_socket_path() -> str:
    """Resolve the default UDS socket from the real transport helper."""
    try:
        from toolrecall.transport import _default_socket_path as _tsp

        return _tsp()
    except Exception:
        # Fall back to the documented default if transport is unavailable.
        return os.path.join(DEFAULT_CACHE_DIR, "toolrecall.sock")


def _pid_file() -> str:
    """Path of the daemon pid file (same as daemon.py PID_FILE)."""
    try:
        from toolrecall.daemon import PID_FILE

        return PID_FILE
    except Exception:
        return os.path.join(DEFAULT_CACHE_DIR, "daemon.pid")


def _lock_glob_dir() -> str:
    return os.path.dirname(_pid_file())


def _forward_proxy_port() -> int:
    """Port the forward proxy binds (same resolution as run_forward_proxy)."""
    try:
        return int(os.environ.get("TOOLRECALL_FORWARD_PORT", "8569"))
    except (TypeError, ValueError):
        return 8569


def proxy_listening(port: int | None = None, timeout: float = 1.0) -> bool:
    """True when the forward proxy accepts TCP connections.

    The proxy is best-effort inside the daemon: if it fails to bind (a restart
    racing the old socket, 2026-09-10) the daemon still reports healthy while
    every LLM call routed through :8569 fails with a connection error. This is
    the signal that was missing — check it explicitly.
    """
    import socket as _socket

    if port is None:
        port = _forward_proxy_port()
    try:
        with _socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


def _proc_transport(pid: int) -> str | None:
    """Return ``pid``'s TOOLRECALL_TRANSPORT env value.

    ``""`` means the process has no override (uses the default socket);
    ``None`` means /proc/<pid>/environ could not be read (other user, zombie,
    hidepid) — the caller must treat that as "unknown, assume ours".
    """
    try:
        with open(f"/proc/{pid}/environ", "rb") as f:
            data = f.read()
    except OSError:
        return None
    for item in data.split(b"\0"):
        if item.startswith(b"TOOLRECALL_TRANSPORT="):
            return item.split(b"=", 1)[1].decode("utf-8", "replace")
    return ""


def count_daemon_procs() -> int:
    """Count live `toolrecall daemon` processes attached to THIS instance.

    Uses the `[t]oolrecall` trailing-anchor trick so the pattern cannot match
    this checker's own command line (which contains the string literally).

    Instances isolated by their own TOOLRECALL_TRANSPORT (bench/AB rigs with a
    separate socket + cache DB) are NOT duplicates of the prod daemon and must
    not be reported as a leaked/duplicate process — 2026-11-07 false alarm:
    a bench daemon under /tmp/trvab.* made the prod healthcheck claim 2 procs.
    """
    try:
        r = subprocess.run(
            ["pgrep", "-af", r"[t]oolrecall daemon"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return -1
    own = _default_socket_path()
    count = 0
    for line in r.stdout.strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            pid = int(line.split(None, 1)[0])
        except (ValueError, IndexError):
            continue
        transport = _proc_transport(pid)
        if transport:  # explicit override -> belongs to another instance
            if transport != own:
                continue
        count += 1
    return count


def _log_path() -> str:
    """Resolve the central rotating log (same resolution as logging_setup)."""
    env = os.environ.get("TOOLRECALL_LOG_FILE")
    if env:
        return os.path.expanduser(env)
    try:
        cfg_dir = os.path.join(DEFAULT_CACHE_DIR, "logs")
        return os.path.join(cfg_dir, "toolrecall.log")
    except Exception:
        return os.path.join(DEFAULT_CACHE_DIR, "logs", "toolrecall.log")


def _scan_recent_log() -> tuple[str, int, int]:
    """Scan the tail of the rotating log for recent ERROR/WARNING counts.

    Bounded: reads at most the last ``TOOLRECALL_LOG_TAIL_KB`` (default 64) KB,
    so a multi-MB log can never slow the healthcheck down. Returns
    (path, error_count, warning_count).
    """
    path = _log_path()
    errors = warnings_n = 0
    try:
        tail_kb = int(os.environ.get("TOOLRECALL_LOG_TAIL_KB", "64"))
    except ValueError:
        tail_kb = 64
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            if size > tail_kb * 1024:
                f.seek(-tail_kb * 1024, os.SEEK_END)
                f.readline()  # drop partial first line
            for raw_line in f:
                line = raw_line.decode("utf-8", errors="replace")
                # Harness daemons (tests) log into the same file with a
                # "[hermetic]" marker. Counting their failures as production
                # health flags the live daemon for someone else's test run —
                # observed 2026-09-26, when 2 of 2 "recent ERRORs" were
                # [hermetic] port races from a sibling session's test rig.
                if "[hermetic]" in line:
                    continue
                # Anchor on " LEVEL " to avoid matching words inside messages
                if " ERROR " in line:
                    errors += 1
                elif " WARNING " in line:
                    warnings_n += 1
    except OSError:
        return ("", 0, 0)
    return (path, errors, warnings_n)


def _probe_shim_state() -> ShimProbe:
    """Classify the shim from one bounded `shim --status --all` probe.

    Three states, because a probe that did not complete is NOT evidence that the
    shim is missing (2026-11-10: a 20 s budget against a 10.8-17.1 s probe
    reported a working shim as `inactive`, and the operator was told to reinstall
    it):

        active    ``applied: yes`` AND ``probe: pass``
        inactive  the probe completed and found no working shim
        unknown   the probe did not complete (timeout / spawn error)

    Both markers are required. A `.pth` that imports but is not applied means
    real interpreters still start unshimmed (2026-11-07 zz_-ordering bug), and an
    applied hook whose probe fails is not serving reads either.
    """
    t0 = time.monotonic()
    try:
        r = subprocess.run(
            _validated_toolrecall_bin() + ["shim", "--status", "--all"],
            capture_output=True,
            text=True,
            timeout=SHIM_PROBE_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        return {
            "shim_state": SHIM_UNKNOWN,
            "shim_reason": "probe_timeout",
            "shim_probe_s": round(time.monotonic() - t0, 2),
            "shim_venvs": [],
            "shim_bare": [],
            "shim_bare_required": [],
        }
    except (subprocess.SubprocessError, OSError):
        return {
            "shim_state": SHIM_UNKNOWN,
            "shim_reason": "probe_error",
            "shim_probe_s": round(time.monotonic() - t0, 2),
            "shim_venvs": [],
            "shim_bare": [],
            "shim_bare_required": [],
        }

    elapsed = round(time.monotonic() - t0, 2)
    return _classify_shim_output(r.stdout or "", elapsed)


def _parse_shim_venvs(out: str) -> list[dict]:
    """Parse ``shim --status --all`` into per-venv rows.

    Block shape::

        ✅ /path/to/venv
             package: ok | shim: present | applied: yes | probe: pass

    A row whose detail line never appears stays bare (``applied``/``probe_ok``
    False): a malformed probe must OVER-report bare venvs, never hide one.
    """
    rows: list[dict] = []
    cur: dict | None = None
    for raw in out.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line[0] in "✅❌":
            cur = {"root": line[1:].strip(), "applied": False, "probe_ok": False}
            rows.append(cur)
            continue
        if cur is not None and line.startswith("package:"):
            cur["applied"] = "applied: yes" in line
            cur["probe_ok"] = "probe: pass" in line
    return rows


def _required_venv_globs() -> list[str]:
    """Required venv globs from config; never fatal to a health check."""
    try:
        from toolrecall.config import load_config

        return list(load_config().shim_required_venvs)
    except Exception:
        log.debug("required-venv config read failed", exc_info=True)
        return []


def _classify_shim_output(out: str, elapsed: float | None = None) -> ShimProbe:
    """Classify one ``shim --status --all`` output into a truthful verdict.

    ``active`` requires a venv with BOTH markers, and no REQUIRED venv bare.
    Required venvs are declared by the operator (``[shim].required_venvs``);
    without that declaration the verdict stays aggregate ("a shim works
    somewhere") but the per-venv rows are reported so a caller can surface the
    gap. The two markers must come from the SAME venv: one venv supplying
    ``applied: yes`` while another supplies ``probe: pass`` is not a working
    shim, and aggregating each marker separately reported ``active`` for a host
    where no venv worked (2026-11-11 review).

    Falls back to the old substring behaviour when the output carries no
    parseable venv blocks (older CLI, unknown format) — never worse than before.
    """
    venvs = _parse_shim_venvs(out)
    if venvs:
        applied = any(v["applied"] for v in venvs)
        probe_pass = any(v["probe_ok"] for v in venvs)
        has_working = any(v["applied"] and v["probe_ok"] for v in venvs)
        bare = [v["root"] for v in venvs if not (v["applied"] and v["probe_ok"])]
    else:
        applied = "applied: yes" in out
        probe_pass = "probe: pass" in out
        has_working = applied and probe_pass
        bare = []
    required = _required_venv_globs()
    bare_required = [p for p in bare if any(fnmatch.fnmatch(p, g) for g in required)]

    if bare_required:
        state, reason = SHIM_INACTIVE, "required_venv_bare"
    elif has_working:
        state, reason = SHIM_ACTIVE, ""
    elif applied:
        state, reason = SHIM_INACTIVE, "probe_failed"
    elif probe_pass:
        state, reason = SHIM_INACTIVE, "applied_missing"
    else:
        state, reason = SHIM_INACTIVE, ""
    return {
        "shim_state": state,
        "shim_reason": reason,
        "shim_probe_s": elapsed,
        "shim_venvs": [
            {"root": v["root"], "applied": bool(v["applied"]), "probe_ok": bool(v["probe_ok"])}
            for v in venvs
        ],
        "shim_bare": bare,
        "shim_bare_required": bare_required,
    }


# ── Hermes-side plugin co-install check ───────────────────────────────────
#
# `toolrecall-light` is a Hermes plugin that hooks `transform_tool_result` and
# stubs byte-identical repeats of ANY tool result. This package dedups file
# content on the bridge/shim, and the proxy rewrites the request itself, so
# running both double-handles the same read: the bridge sends full content, the
# plugin replaces it with its own stub, and our mcp_dedup counters then credit a
# full send the model never received. One of the two belongs off.

LIGHT_PLUGIN_NAME = "toolrecall-light"


def _hermes_home_path() -> str:
    return os.environ.get("HERMES_HOME") or os.path.join(os.path.expanduser("~"), ".hermes")


def detect_light_plugin(home: str | None = None) -> dict:
    """Best-effort detection of the Hermes ``toolrecall-light`` plugin.

    Never raises and never inspects plugin internals: an installed directory is
    ``installed``, a ``- toolrecall-light`` entry in config.yaml is ``enabled``
    (``plugins.enabled`` is a plain YAML list, so the list item is unambiguous
    without a YAML parser this repo deliberately does not depend on).
    """
    base = home or _hermes_home_path()
    plugin_dir = os.path.join(base, "plugins", LIGHT_PLUGIN_NAME)
    try:
        installed = os.path.isdir(plugin_dir)
    except OSError:
        installed = False
    enabled = False
    try:
        with open(os.path.join(base, "config.yaml"), encoding="utf-8", errors="replace") as fh:
            text = fh.read()
        enabled = any(line.strip() == f"- {LIGHT_PLUGIN_NAME}" for line in text.splitlines())
        installed = installed or enabled
    except OSError:
        pass
    return {"installed": installed, "enabled": enabled, "path": plugin_dir if installed else ""}


def gather() -> HealthInfo:
    """Collect every health signal into one dict. No side effects."""
    pid_file = _pid_file()
    lock_dir = _lock_glob_dir()

    info: HealthInfo = {
        "procs": count_daemon_procs(),
        "pid_file": "",
        "pid_alive": False,
        "lock_files": 0,
        "socket_path": _default_socket_path(),
        "socket": False,
        "proxy": False,
        "proxy_port": _forward_proxy_port(),
        "shim_state": SHIM_INACTIVE,
        "shim_reason": "",
        "shim_probe_s": None,
        "shim_venvs": [],
        "shim_bare": [],
        "shim_bare_required": [],
        "hit_rate": None,
        "log_file": "",
        "log_errors_recent": 0,
        "log_warnings_recent": 0,
        "hermes_plugin": detect_light_plugin(),
    }

    if os.path.isfile(pid_file):
        try:
            with open(pid_file) as f:
                info["pid_file"] = f.read().strip()
        except OSError:
            info["pid_file"] = "?"

    pid = info["pid_file"]
    if pid.isdigit():
        info["pid_alive"] = _pid_alive(int(pid))
    elif pid:
        info["pid_alive"] = False

    if os.path.isdir(lock_dir):
        try:
            info["lock_files"] = len(
                [e for e in os.listdir(lock_dir) if e.startswith("daemon-") and e.endswith(".lck")]
            )
        except OSError:
            pass

    sock = info["socket_path"]
    if os.path.exists(sock) or os.path.islink(sock):
        try:
            info["socket"] = os.stat(sock).st_mode != 0
        except OSError:
            info["socket"] = False

    # Forward proxy listener (best-effort probe — the gap that let a dead
    # proxy look like a healthy daemon on 2026-09-10).
    info["proxy"] = proxy_listening(info.get("proxy_port"))

    # Best-effort shim state (one bounded CLI probe; see _probe_shim_state).
    probe = _probe_shim_state()
    info["shim_state"] = probe["shim_state"]
    info["shim_reason"] = probe["shim_reason"]
    info["shim_probe_s"] = probe["shim_probe_s"]
    info["shim_venvs"] = probe["shim_venvs"]
    info["shim_bare"] = probe["shim_bare"]
    info["shim_bare_required"] = probe["shim_bare_required"]

    # Best-effort hit rate from stats (bounded; direct mode trace is fine).
    # NO_AUTOSTART is mandatory here: `toolrecall stats` is in the CLI's
    # daemon-required set, so without it a *health check* spawns the daemon it
    # is supposed to diagnose — which rewrites daemon.pid and the instance
    # lock mid-run and made `--clean-stale` race its own report.
    try:
        r = subprocess.run(
            _validated_toolrecall_bin() + ["stats"],
            capture_output=True,
            text=True,
            timeout=25,
            env={**os.environ, "PYTHONUNBUFFERED": "1", "TOOLRECALL_NO_AUTOSTART": "1"},
        )
        if r.returncode == 0:
            import json as _json

            data = _json.loads(r.stdout)
            cats = {
                k: v
                for k, v in data.items()
                if isinstance(v, dict) and "hits" in v and "misses" in v
            }
            h = sum(c.get("hits", 0) for c in cats.values())
            m = sum(c.get("misses", 0) for c in cats.values())
            if (h + m) > 0:
                info["hit_rate"] = round(h / (h + m), 3)
    except Exception:
        info["hit_rate"] = None

    # Bounded log-tail scan: recent ERROR/WARNING volume (log-level watch).
    info["log_file"], info["log_errors_recent"], info["log_warnings_recent"] = _scan_recent_log()

    return info


# ── Stale-state inspection & cleanup (`healthcheck --clean-stale`) ────────


class CleanupReport(TypedDict):
    pid_file: str
    pid_file_removed: bool
    pid_file_reason: str
    lock_files_removed: list[str]
    lock_files_kept: int


def _proc_state(pid: int) -> str:
    """Classify a PID as 'alive' | 'zombie' | 'dead' | 'unknown'.

    A ``/proc/<pid>`` entry is NOT liveness: a zombie keeps its entry until
    reaped, and a recycled PID keeps it under a different program. The old
    presence-only check therefore reported a dead daemon as alive in exactly
    the two cases stale-cleanup exists for.

    Without /proc we ask the kernel with signal 0 — but never on Windows,
    where ``os.kill(pid, 0)`` is ``TerminateProcess`` and would *kill* the
    process we are probing.
    """
    if pid <= 0:
        return "dead"
    if os.path.isdir("/proc"):
        try:
            with open(f"/proc/{pid}/stat", "rb") as f:
                data = f.read().decode("utf-8", "replace")
        except FileNotFoundError:
            return "dead"
        except OSError:
            return "unknown"
        # Field 3 (state) follows the parenthesized comm, which may itself
        # contain spaces and parentheses — split after the LAST ')'.
        rparen = data.rfind(")")
        state = data[rparen + 2 : rparen + 3] if rparen != -1 else ""
        if state == "Z":
            return "zombie"
        if state in ("X", "x"):
            return "dead"
        return "alive"
    if os.name == "posix":
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return "dead"
        except PermissionError:
            return "alive"
        except OSError:
            return "unknown"
        return "alive"
    return "unknown"


def _pid_alive(pid: int) -> bool:
    """True unless the PID is provably dead or a zombie.

    'unknown' counts as alive: on a platform where liveness cannot be
    verified, the healthcheck must not cry "stale daemon.pid".
    """
    return _proc_state(pid) not in ("dead", "zombie")


def _is_toolrecall_process(pid: int) -> bool | None:
    """Whether /proc/<pid>/cmdline names toolrecall; None when undecidable.

    Only used to recognize PID reuse: a live PID recorded in daemon.pid whose
    command line is readable and has nothing to do with ToolRecall cannot be
    the daemon that wrote that file.
    """
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            raw = f.read()
    except OSError:
        return None
    argv = [chunk for chunk in raw.decode("utf-8", "replace").split("\0") if chunk]
    if not argv:
        return None  # kernel thread or zombie: no argv to judge by
    return any("toolrecall" in arg.lower() for arg in argv)


def _stale_pid_reason(pid_text: str) -> str | None:
    """Why ``pid_text`` is provably not a live daemon PID, or None if it is.

    "Provably" is the contract: every branch either proves the recorded
    daemon is gone or declines to touch the file.
    """
    pid_text = pid_text.strip()
    if not pid_text:
        return None
    if not pid_text.isdigit():
        return f"invalid pid {pid_text!r}"
    pid = int(pid_text)
    if pid <= 0:
        return f"invalid pid {pid}"
    state = _proc_state(pid)
    if state == "dead":
        return f"pid {pid} not alive"
    if state == "zombie":
        return f"pid {pid} is a zombie (never reaped)"
    if state == "unknown":
        return None  # cannot prove death → leave it alone
    if _is_toolrecall_process(pid) is False:
        return f"pid {pid} is alive but not a toolrecall daemon (pid reused)"
    return None


def _try_acquire_lock(path: str, create: bool = True):
    """Open *path* and take a non-blocking exclusive lock.

    Returns the open file object when the lock is ours, None when a live
    process holds it. Raises FileNotFoundError when ``create=False`` and the
    file vanished (an already-swept orphan).
    """
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    fh = open(path, "a+" if create else "r+")
    try:
        if os.name == "nt":
            import msvcrt

            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)  # type: ignore[attr-defined]  # Windows-only
        else:
            import fcntl

            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return None
    # Record the holder PID for humans/debuggers, like daemon.py does.
    try:
        fh.seek(0)
        fh.truncate()
        fh.write(str(os.getpid()))
        fh.flush()
    except OSError as e:
        # The lock itself is already ours; only the diagnostic PID is missing.
        log.debug("could not record lock holder pid in %s: %s", path, e)
    return fh


def _drop_lock(fh, unlink: bool = True) -> None:
    """Release a lock taken by ``_try_acquire_lock`` and unlink its file."""
    if unlink:
        try:
            os.unlink(fh.name)
        except FileNotFoundError:
            pass  # already gone (raced with another sweeper)
        except OSError as e:
            log.debug("instance-lock unlink skipped for %s: %s", fh.name, e)
    try:
        fh.close()
    except OSError as e:
        log.debug("instance-lock close failed for %s: %s", fh.name, e)


def _sweep_lock_files(lock_dir: str) -> tuple[list[str], int]:
    """Unlink flock-able ``daemon-*.lck`` files; return (removed, kept).

    Same safety rule as ``daemon._sweep_stale_lock_files``: the kernel lock,
    not the recorded PID, decides. A file we can flock has no live holder;
    a file we cannot flock is never touched, whatever its content says.
    """
    removed: list[str] = []
    kept = 0
    if not os.path.isdir(lock_dir):
        return removed, kept
    for name in sorted(os.listdir(lock_dir)):
        if not (name.startswith("daemon-") and name.endswith(".lck")):
            continue
        path = os.path.join(lock_dir, name)
        try:
            fh = _try_acquire_lock(path, create=False)
        except FileNotFoundError:
            continue  # raced with another sweeper — success case
        except OSError:
            kept += 1
            continue
        if fh is None:
            kept += 1  # live holder
            continue
        try:
            os.unlink(path)
            removed.append(path)
        except FileNotFoundError:
            pass  # raced with another sweeper — success case
        except OSError:
            kept += 1
        finally:
            _drop_lock(fh, unlink=False)
    return removed, kept


def cleanup_stale_state() -> CleanupReport:
    """Remove a provably dead ``daemon.pid`` and orphaned ``daemon-*.lck``.

    Safety model — fail closed at every step:

    1. A lock file is removed only when its flock can be taken non-blocking;
       the kernel decides whether a holder is alive, never the recorded PID.
    2. The PID file is removed only when its PID is provably dead: invalid
       content, no /proc entry, a zombie, or a live PID whose command line is
       readable and belongs to another program (PID reuse).
    3. PID removal runs under the production instance lock, so a daemon
       starting concurrently either wins first (we then leave the file
       alone) or loses and exits — it can never write a PID file that we
       unlink afterwards.

    Never touches the socket, the cache DB, or a held lock.
    """
    pid_path = _pid_file()
    report: CleanupReport = {
        "pid_file": pid_path,
        "pid_file_removed": False,
        "pid_file_reason": "",
        "lock_files_removed": [],
        "lock_files_kept": 0,
    }

    pid_text: str | None = None  # None = no pid file at all
    if os.path.isfile(pid_path):
        try:
            with open(pid_path) as f:
                pid_text = f.read().strip()
        except OSError as e:
            report["pid_file_reason"] = f"unreadable: {e}"

    reason = _stale_pid_reason(pid_text) if pid_text is not None else None
    if reason:
        # Fail closed: no gate, no removal. Resolving the path or opening the
        # lock may raise (broken install, read-only dir); let it surface as a
        # hard healthcheck error instead of deleting unguarded.
        from toolrecall.daemon import _instance_lock_path

        gate_path = _instance_lock_path(_default_socket_path())
        gate = _try_acquire_lock(gate_path)
        if gate is None:
            report["pid_file_reason"] = (
                "daemon holds the instance lock (running or starting) — pid file left untouched"
            )
        else:
            try:
                # Re-read under the gate: a daemon that started between our
                # first read and the lock acquisition has rewritten the file.
                try:
                    with open(pid_path) as f:
                        current = f.read().strip()
                except OSError:
                    current = ""
                reason = _stale_pid_reason(current) if current else None
                if reason:
                    try:
                        os.remove(pid_path)
                        report["pid_file_removed"] = True
                        report["pid_file_reason"] = reason
                    except FileNotFoundError:
                        report["pid_file_reason"] = "already removed"
                    except OSError as e:
                        report["pid_file_reason"] = f"remove failed: {e}"
            finally:
                _drop_lock(gate)

    report["lock_files_removed"], report["lock_files_kept"] = _sweep_lock_files(
        os.path.dirname(pid_path)
    )
    return report


def cleanup_line(report: CleanupReport) -> str:
    """One-line summary of what ``--clean-stale`` did."""
    if report["pid_file_removed"]:
        pid_part = f"removed stale daemon.pid ({report['pid_file_reason']})"
    elif report["pid_file_reason"]:
        pid_part = f"daemon.pid kept — {report['pid_file_reason']}"
    else:
        pid_part = "no stale daemon.pid"
    n = len(report["lock_files_removed"])
    if n:
        lock_part = f"removed {n} orphaned lock file{'s' if n != 1 else ''}"
    else:
        lock_part = "no orphaned lock files"
    if report["lock_files_kept"]:
        lock_part += f" ({report['lock_files_kept']} held, kept)"
    return f"cleanup: {pid_part}; {lock_part}"


def warnings(info: HealthInfo) -> list[str]:
    """Turn gathered signals into a list of human-readable warnings."""
    notes = []
    procs = info["procs"]
    if procs < 0:
        notes.append("daemon procs: unknown (pgrep unavailable)")
    elif procs == 0:
        notes.append("daemon NOT running (0 procs)")
    elif procs > 1:
        notes.append(f"daemon: {procs} procs — expected 1 (duplicate/leaked)")

    pid = info["pid_file"]
    if pid:
        if not info["pid_alive"]:
            notes.append(f"stale daemon.pid (pid {pid} not alive)")
    else:
        notes.append("no daemon.pid file present")

    if info["lock_files"] > 2:
        notes.append(
            f"{info['lock_files']} lock files (daemon-*.lck) — expected ≤2; "
            "stale locks never cleaned"
        )

    if not info["socket"]:
        notes.append("UDS socket not present")

    if not info.get("proxy", True):
        notes.append(
            f"forward proxy NOT listening on :{info.get('proxy_port')} — "
            "LLM traffic routed through it fails with connection errors"
        )

    plugin = info.get("hermes_plugin") or {}
    if plugin.get("enabled"):
        notes.append(
            "toolrecall-light (Hermes plugin) is installed AND enabled — both layers "
            "dedup the same tool results, so mcp_dedup counts overstate what the model "
            "actually received; run one, not both (`hermes plugins disable "
            "toolrecall-light` keeps this stack)"
        )

    if info.get("shim_state") == SHIM_UNKNOWN:
        if info.get("shim_reason") == "probe_timeout":
            took = info.get("shim_probe_s")
            budget = f"{took:.1f}s" if isinstance(took, (int, float)) else "the budget"
            notes.append(
                f"shim probe timed out after {budget}"
                " — state unknown, NOT missing; the shim may be fine, re-run"
                " `toolrecall shim --status --all`"
            )
        else:
            notes.append(
                "shim probe could not run — state unknown, NOT missing; re-run"
                " `toolrecall shim --status --all`"
            )

    bare_required = info.get("shim_bare_required") or []
    if bare_required:
        notes.append(
            "shim: required venv(s) NOT shimmed — "
            + ", ".join(bare_required[:3])
            + " (reads from that interpreter bypass the cache and the context tier)"
        )
    elif info.get("shim_state") == SHIM_ACTIVE:
        bare = info.get("shim_bare") or []
        if bare:
            shown = ", ".join(bare[:2]) + ("…" if len(bare) > 2 else "")
            notes.append(
                f"shim: {len(bare)} of {len(info.get('shim_venvs') or bare)} discovered venv(s)"
                f" unshimmed — {shown}; declare [shim].required_venvs to make this fail"
            )

    log_err = info.get("log_errors_recent", 0)
    log_warn = info.get("log_warnings_recent", 0)
    if log_err:
        notes.append(
            f"log: {log_err} ERRORs in last {os.environ.get('TOOLRECALL_LOG_TAIL_KB', '64')}KB"
            f" ({os.path.basename(info['log_file']) or 'log missing'})"
        )
    if log_warn >= 10:
        notes.append(
            f"log: {log_warn} WARNINGs in last {os.environ.get('TOOLRECALL_LOG_TAIL_KB', '64')}KB"
            " — noisy or degrading"
        )

    return notes


def render(info: HealthInfo) -> str:
    """One-line human summary (mirrors the cron healthcheck's format)."""
    pid = info["pid_file"] or "none"
    age = "" if info["hit_rate"] is None else f" hit_rate={info['hit_rate'] * 100:.0f}%"
    log_seg = ""
    if info.get("log_file"):
        log_seg = f" | log={info['log_errors_recent']}e/{info['log_warnings_recent']}w"
    shim_detail = ""
    if info.get("shim_reason"):
        shim_detail = f"({info['shim_reason']})"
    bare_n = len(info.get("shim_bare") or [])
    if bare_n:
        shim_detail += f" bare={bare_n}"
    return (
        f"toolrecall healthcheck | procs={info['procs']} | pid={pid} | "
        f"locks={info['lock_files']} | sock={'yes' if info['socket'] else 'no'} "
        f"| proxy={'yes' if info.get('proxy') else 'NO'} "
        f"| shim={info['shim_state']}{shim_detail}{age}{log_seg}"
    )


def run(as_json: bool = False, clean_stale: bool = False) -> int:
    """Execute the healthcheck and print the report. Returns the exit code.

    ``as_json=True`` prints a single JSON object (summary over {"health": {...},
    "warnings": [...]}) instead of the human line, for cron/script consumption.
    ``clean_stale=True`` removes provably stale daemon state first (see
    :func:`cleanup_stale_state`) and reports what was removed.
    """
    cleanup = cleanup_stale_state() if clean_stale else None
    info = gather()
    notes = warnings(info)
    if as_json:
        import json as _json

        payload: dict[str, object] = {"health": info, "warnings": notes}
        if cleanup is not None:
            payload["cleanup"] = cleanup
        print(_json.dumps(payload, default=str))
    else:
        if cleanup is not None:
            print(cleanup_line(cleanup))
        print(render(info))
        if notes:
            print(f"  note: {'; '.join(notes)}")
    if info["procs"] == 0:
        return EXIT_WARN
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(run("--json" in sys.argv))
