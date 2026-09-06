"""ToolRecall auto-updater — opt-out (default ON), zero runtime dependencies.

Design (see .hermes/plans/2026-09-04_213857-toolrecall-autoupdater.md):

- Check runs at CLI entry (main()), NEVER in the daemon request path —
  the daemon stays fully offline.
- PyPI JSON API via stdlib urllib; 1.0s timeout; any failure degrades to
  "skip" and is RECORDED in the state file (auditable via `toolrecall
  update --status`), never silently swallowed.
- Interval-gated: at most one network check per `check_interval_hours`
  (default 24h). A failed check still counts as "checked" — no hot-loop
  retries when offline.
- Upgrade via the SAME interpreter's pip (`python -m pip install
  --upgrade toolrecall`); uv fallback mirrors venvs._install_toolrecall.
- Editable/dev installs are hard-skipped (direct_url.json probe) — the
  updater never clobbers a dev checkout.
- Opt-out: [update] enabled = false in TOML, or TOOLRECALL_UPDATE_CHECK=0
  env (env wins, matching the global env > TOML priority).
"""

import json
import os
import subprocess
import sys
import time
import urllib.request

from toolrecall import __version__ as _VERSION

_PYPI_URL = "https://pypi.org/pypi/toolrecall/json"
_CHECK_TIMEOUT_S = 1.0
_PIP_TIMEOUT_S = 120
_DEFAULT_INTERVAL_H = 24


# ─── State file ───────────────────────────────────────────


def _state_path() -> str:
    override = os.environ.get("TOOLRECALL_UPDATE_STATE")
    if override:
        return override
    return os.path.join(os.path.expanduser("~/.toolrecall"), "last_update_check.json")


def _load_state() -> dict:
    try:
        with open(_state_path()) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        # Missing or corrupt state = fresh start. Not a silent swallow: an
        # absent state file IS the documented "never checked" state.
        return {}


def _save_state(state: dict) -> None:
    path = _state_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp, path)  # atomic — no half-written state
    except OSError as e:
        import warnings

        warnings.warn(f"ToolRecall updater: could not write state file {path}: {e}")


# ─── Version compare (stdlib-only semver subset) ──────────


def _version_key(v: str) -> tuple:
    """Parse 'X.Y.Z<suffix>' into a sortable tuple.

    Numeric parts sort naturally; a non-empty suffix (rc1, dev0, b2) sorts
    BELOW the bare release (1.0.0rc1 < 1.0.0). Garbage degrades to (0,)
    rather than raising. Trailing .0 padding makes (0,8,9) == (0,8,9,0)
    compare correctly against longer tuples.
    """
    if not isinstance(v, str) or not v:
        return (0,)
    digits, has_suffix = [], False
    for part in v.strip().split("."):
        num = ""
        for ch in part:
            if ch.isdigit():
                num += ch
            else:
                break
        if num and len(num) < len(part):
            has_suffix = True  # e.g. '0rc1' — digits followed by letters
        if num:
            digits.append(int(num))
        elif part:
            has_suffix = True  # pure suffix part, e.g. 'rc1' or 'dev0'
    if not digits:
        return (0,)
    key = tuple(digits)
    # Pad + tag: pre-releases sort below their bare release
    # (1.0.0rc1 → (1,0,0,-1) < (1,0,0) → (1,0,0,0)); the trailing tag also
    # normalizes cross-length compares.
    return key + ((-1,) if has_suffix else (0,))


# ─── PyPI check ───────────────────────────────────────────


def _pypi_latest() -> str | None:
    """Fetch the latest published version from PyPI (None on any failure)."""
    try:
        req = urllib.request.Request(_PYPI_URL, headers={"User-Agent": f"toolrecall/{_VERSION}"})
        with urllib.request.urlopen(req, timeout=_CHECK_TIMEOUT_S) as r:
            data = json.load(r)
        v = data.get("info", {}).get("version")
        return v if isinstance(v, str) and v else None
    except Exception:
        # Offline / DNS / timeout / blocked egress / schema drift. Recorded
        # by the caller in the state file — never invisible.
        return None


def _update_enabled(cfg) -> bool:
    """[update] enabled (default True); TOOLRECALL_UPDATE_CHECK env wins.

    Delegates to Config.update_enabled — single source of truth for the
    env-var semantics (also used by config-set validation).
    """
    if cfg is not None:
        return cfg.update_enabled
    # No config object: apply env directly, else default ON.
    env = os.environ.get("TOOLRECALL_UPDATE_CHECK")
    if env is not None:
        return env.strip().lower() not in ("0", "false", "no", "off")
    return True


def _interval_seconds(cfg) -> float:
    hours: float = float(_DEFAULT_INTERVAL_H)
    if cfg is not None:
        try:
            hours = float(cfg.get("update", "check_interval_hours", default=24))
        except (TypeError, ValueError):
            pass
    return max(hours, 0.0) * 3600.0


def check_for_update(cfg, force: bool = False) -> str | None:
    """Return the latest version if newer than installed, else None.

    Side effect: refreshes the state file (last_check / latest_seen /
    last_check_failed). Respects [update] enabled + interval unless forced.
    """
    if not _update_enabled(cfg):
        return None

    state = _load_state()
    if not force:
        elapsed = time.time() - state.get("last_check", 0)
        if elapsed < _interval_seconds(cfg):
            return None

    latest = _pypi_latest()
    state["last_check"] = time.time()
    state["last_check_failed"] = latest is None
    if latest:
        state["latest_seen"] = latest
        state.pop("last_check_failed", None)
    _save_state(state)

    if latest and _version_key(latest) > _version_key(_VERSION):
        return latest
    return None


# ─── Install mechanics ────────────────────────────────────


def _is_editable_install() -> bool:
    """True when toolrecall is an editable/local install — never auto-upgrade.

    direct_url.json records the origin for PEP 660 editable installs and
    local file:// installs. Missing file = normal PyPI install.
    """
    try:
        from importlib.metadata import distribution

        raw = distribution("toolrecall").read_text("direct_url.json")
    except Exception:
        return False  # not installed via metadata we can read → treat as PyPI
    if not raw:
        return False
    try:
        url = json.loads(raw).get("url", "")
    except ValueError:
        return True  # unparseable origin → be conservative, skip
    return url.startswith("file://") or "editable" in raw


def _pipx_venv_root() -> str | None:
    """Absolute pipx shared-venvs root when THIS interpreter runs in a pipx venv.

    Detection: sys.prefix lives under pipx's venvs dir ($PIPX_LOCAL_VENVS,
    default ~/.local/pipx/venvs) AND the leaf dir is named like a pipx
    package venv (contains a <name>.dist-info sibling of toolrecall's).
    Returns the root (e.g. /home/u/.local/pipx/venvs) or None.
    """
    prefix = os.path.realpath(sys.prefix)
    custom = os.environ.get("PIPX_LOCAL_VENVS")
    roots = [os.path.realpath(os.path.expanduser(custom))] if custom else []
    roots.append(os.path.realpath(os.path.expanduser("~/.local/pipx/venvs")))
    for root in roots:
        if prefix.startswith(root + os.sep) and prefix != root:
            return root
    return None


def _pip_cmd() -> list[str]:
    """Upgrade command for THIS environment.

    - Inside a pipx venv: prefer the pipx manager (`pipx upgrade
      toolrecall`) so pipx's bookkeeping stays consistent — naked pip in a
      managed venv works but desyncs `pipx metadata`/reinstall state.
      pipx binary resolved via shutil.which; falls back to pip when absent.
    - Otherwise: [python, -m, pip] for THIS interpreter (venvs.py pattern:
      same env).
    """
    import shutil

    if _pipx_venv_root() is not None:
        pipx = shutil.which("pipx")
        if pipx:
            return [pipx, "upgrade", "toolrecall"]
    return [sys.executable, "-m", "pip"]


def apply_update(latest: str) -> bool:
    """Upgrade toolrecall via this interpreter's pip. Records the outcome.

    Returns True only when pip exits 0. Editable installs are skipped.
    Running processes keep their imported code — only newly started
    processes (the restarted daemon) pick up the new version.
    """
    state = _load_state()

    if _is_editable_install():
        state["last_update_status"] = "skipped_editable"
        state["last_update_attempt"] = time.time()
        _save_state(state)
        return False

    try:
        r = subprocess.run(
            _pip_cmd() + ["install", "--upgrade", "toolrecall"],
            capture_output=True,
            text=True,
            timeout=_PIP_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        state["last_update_status"] = "failed"
        state["last_update_error"] = str(e)[:200]
        state["last_update_attempt"] = time.time()
        _save_state(state)
        return False

    state["last_update_attempt"] = time.time()
    if r.returncode == 0:
        state["last_update_status"] = "updated"
        state["last_update_to"] = latest
        state.pop("last_update_error", None)
    else:
        state["last_update_status"] = "failed"
        state["last_update_error"] = (r.stderr or r.stdout or "")[-200:]
    _save_state(state)
    return r.returncode == 0


def update_status_line() -> str | None:
    """One-line status for `toolrecall status` — surfaces updater state."""
    state = _load_state()
    if not state:
        return None
    parts = []
    lc = state.get("last_check")
    if lc:
        age_h = (time.time() - lc) / 3600
        parts.append(f"last checked {age_h:.0f}h ago")
    if state.get("last_check_failed"):
        parts.append("last check FAILED (offline?)")
    seen = state.get("latest_seen")
    if seen and _version_key(seen) > _version_key(_VERSION):
        parts.append(f"update pending: {seen} (installed {_VERSION})")
    status = state.get("last_update_status")
    if status and status != "updated":
        parts.append(f"last update: {status}")
    return " · ".join(parts) if parts else None


def run_update_cli(force_check: bool, apply: bool) -> None:
    """Entry for `toolrecall update [--check]`."""
    from toolrecall.config import load_config

    cfg = load_config()
    if not _update_enabled(cfg):
        print("  ❌ Auto-update is disabled ([update] enabled = false or")
        print("     TOOLRECALL_UPDATE_CHECK=0). Re-enable to use this command.")
        sys.exit(1)

    print(f"  Installed: {_VERSION}")
    print("  Checking PyPI...", end="", flush=True)
    latest = check_for_update(cfg, force=force_check)
    print(" done")

    if not latest:
        seen = _load_state().get("latest_seen")
        if force_check and seen:
            print(f"  ✅ Up to date (latest on PyPI: {seen})")
        else:
            print("  ✅ Up to date (or PyPI unreachable — see `toolrecall update --status`)")
        return

    print(f"  ⬆️  New version available: {latest}")
    if not apply:
        print("  Run `toolrecall update` to install it.")
        return

    if _is_editable_install():
        print("  ⚠️  Editable/dev install detected — auto-upgrade skipped.")
        print("     Upgrade via git pull + pip install -e . instead.")
        return

    print("  Running pip upgrade...", end="", flush=True)
    ok = apply_update(latest)
    print(" done")
    if ok:
        print(f"  ✅ Updated to {latest} — run `toolrecall restart` to apply to the daemon.")
    else:
        print("  ❌ Update failed — see ~/.toolrecall/last_update_check.json")
        print("     and `toolrecall update --status` for details.")


def run_status_cli() -> None:
    """Entry for `toolrecall update --status`."""
    line = update_status_line()
    print(f"  Installed: {_VERSION}")
    if line:
        print(f"  Updater: {line}")
    else:
        print("  Updater: no checks recorded yet")
