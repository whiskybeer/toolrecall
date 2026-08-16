"""Generic Python venv discovery + .pth shim helper (agent-agnostic).

The transparent cache shim works by dropping a ``tr_shim.pth`` file into a
Python environment's ``site-packages``; at interpreter startup that file runs
``toolrecall.shim.apply()`` and patches ``open()``/``subprocess`` for every
process in that venv. Historically ``toolrecall setup`` only ever shimmed the
environment ``toolrecall`` itself ran from (a pipx/uv isolated venv that agents
never see), which is why the healthcheck showed ``mcp_hits=0, term_hits=0``.

This module is the agent-agnostic replacement: it discovers *all* active Python
environments on the machine, installs the ``toolrecall`` package into the chosen
one if missing, drops the ``.pth``, and — crucially — **probes** the result so a
silently-broken shim (package absent, bad site-packages path) is caught instead
of being reported as "active".
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from typing import List, Optional

#: Paths under $HOME known to host agent venvs. Scanned in addition to the
#: generic ``*/bin/python3* + pyvenv.cfg`` walk. Be generous here; each hit is
#: deduped by realpath and validated by asking the interpreter itself.
_AGENT_ROOT_HINTS = (
    ".hermes",
    ".local/share/uv/tools",
    ".local/share/pipx/venvs",
    ".opencode",
    ".codex",
    ".cursor",
)

#: Directories that are never worth walking for venvs.
_SKIP_DIRNAMES = {
    ".cache",
    ".git",
    ".npm",
    "node_modules",
    "site-packages",
    ".venv/lib",
    "lib",
    "bin",
    "__pycache__",
    ".gradle",
    "target",
}


@dataclass
class Venv:
    """A discovered/represented Python environment."""

    root: str
    python: str  # absolute path to the venv's interpreter
    site_packages: str  # resolved site-packages dir (may be '' if unprobeable)


def _real(path: str) -> str:
    try:
        return os.path.realpath(path)
    except OSError:
        return path


def _site_packages_of(python: str, timeout: float = 10.0) -> Optional[str]:
    """Ask the target interpreter for its real site-packages dir.

    Uses ``cwd=`` (a temp dir) so the interpreter's own sys.path is used, not
    any cwd-based ``''`` entry that would lie about the environment.
    """
    code = (
        "import site,sys;"
        "print([p for p in site.getsitepackages() if p.endswith('site-packages')][0])"
    )
    try:
        with tempfile.TemporaryDirectory(prefix="tr-sp-") as td:
            r = subprocess.run(
                [python, "-c", code],
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=td,
            )
        if r.returncode == 0:
            sp = r.stdout.strip()
            return sp if sp and os.path.isdir(sp) else None
    except (OSError, subprocess.TimeoutExpired):
        pass
    return None


def _is_toolrecall_own(py_path: str) -> bool:
    """True if ``toolrecall`` resolves from this interpreter == this codebase.

    Used to exclude toolrecall's own envs (pipx venv, repo .venv, uv tool env)
    from discovery. When ``toolrecall`` is installed here this resolves inside
    the running source tree; when it isn't importable we conservatively return
    False (the env is not toolrecall's own, or can't be proved to be).
    """
    try:
        import toolrecall

        here = _real(os.path.dirname(os.path.dirname(toolrecall.__file__)))
    except Exception:
        return False
    return _real(py_path).startswith(here)


def _python_from_running_procs() -> List[str]:
    """Resolve interpreters of live Python processes via /proc (Linux)."""
    pythons = []
    try:
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            exe = f"/proc/{entry}/exe"
            try:
                real = os.path.realpath(exe)
            except OSError:
                continue
            if real and os.path.basename(real).startswith("python"):
                pythons.append(real)
    except (FileNotFoundError, PermissionError):
        pass
    return pythons


def _candidate_pythons_from_home() -> List[str]:
    """Walk $HOME for venv launcher scripts (agent roots + generic).

    A proper venv is identified by a ``pyvenv.cfg`` at its root. For each such
    cfg we resolve the launcher script ``<root>/bin/python3`` — this avoids
    any ambiguity about whether ``bin/`` etc. are traversed.
    """
    home = os.path.expanduser("~")
    cfg_files = []

    def scan_root(start: str, max_depth: int) -> None:
        if not os.path.isdir(start):
            return
        try:
            for root, dirs, files in os.walk(start):
                dirs[:] = [d for d in dirs if d not in _SKIP_DIRNAMES]
                if "pyvenv.cfg" in files:
                    cfg_files.append(os.path.join(root, "pyvenv.cfg"))
                depth = root[len(start) :].count(os.sep)
                if depth >= max_depth:
                    dirs[:] = []
        except OSError:
            pass

    for base in _AGENT_ROOT_HINTS:
        scan_root(os.path.join(home, base), max_depth=6)
    # Generic: any venv directly under $HOME (depth ~2: myvenv/pyvenv.cfg)
    scan_root(home, max_depth=3)

    out = []
    for cfg in cfg_files:
        root = os.path.dirname(cfg)
        bin_dir = os.path.join(root, "bin")
        if not os.path.isdir(bin_dir):
            continue
        launcher = None
        for name in sorted(os.listdir(bin_dir)):
            if name.startswith("python3"):
                full = os.path.join(bin_dir, name)
                if os.access(full, os.X_OK):
                    launcher = full
                    break
        if launcher:
            out.append(launcher)
    return out


def discover_python_venvs() -> List[Venv]:
    """Discover all active Python environments on the machine.

    Sources (deduped by interpreter realpath):
      1. Live ``python`` processes (from ``/proc/<pid>/exe``).
      2. ``$HOME`` walk for agent-root venvs + generic ``*/bin/python3*`` w/
         an adjacent ``pyvenv.cfg``.
    Always excludes toolrecall's own environment(s).
    """
    seen: set = set()
    out: List[Venv] = []

    raw = _python_from_running_procs() + _candidate_pythons_from_home()

    for py in raw:
        py = _real(py)
        if not py or py in seen or _is_toolrecall_own(py):
            continue
        seen.add(py)
        sp = _site_packages_of(py)
        if not sp:
            continue  # not truly a probeable venv / toolrecall-free env
        root = os.path.dirname(os.path.dirname(py))
        out.append(Venv(root=root, python=py, site_packages=sp))
    return out


def _importable(python: str, mod: str) -> bool:
    """Probe whether ``mod`` imports cleanly from a neutral cwd."""
    try:
        with tempfile.TemporaryDirectory(prefix="tr-probe-") as td:
            r = subprocess.run(
                [python, "-c", f"import {mod}"],
                capture_output=True,
                text=True,
                timeout=20,
                cwd=td,
            )
        return r.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _install_toolrecall(venv: Venv) -> bool:
    """Install the ``toolrecall`` package into the target venv.

    Prefers ``pip`` present in the venv; falls back to ``uv pip install`` with
    ``--python`` if pip is absent but uv is available. Read-only/cached uv
    venvs that can't be written are caught and reported False.
    """
    pip = os.path.join(os.path.dirname(venv.python), "pip")
    if os.access(pip, os.X_OK):
        try:
            r = subprocess.run(
                [pip, "install", "toolrecall"],
                capture_output=True,
                text=True,
                timeout=120,
            )
            return r.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False
    uv = shutil.which("uv")
    if uv:
        try:
            r = subprocess.run(
                [uv, "pip", "install", "--python", venv.python, "toolrecall"],
                capture_output=True,
                text=True,
                timeout=120,
            )
            return r.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False
    return False


def _pth_source() -> str:
    return os.path.join(os.path.dirname(__file__), "tr_shim.pth")


def ensure_shim(venv: Venv) -> bool:
    """Install the shim into ``venv`` idempotently and verify by probe.

    Steps:
      1. Ensure ``toolrecall`` is importable in the venv (install if missing).
      2. Copy ``tr_shim.pth`` into ``venv.site_packages``.
      3. Probe ``import toolrecall.shim`` from a neutral cwd.

    Returns True only if the final probe passes.
    """
    os.makedirs(venv.site_packages, exist_ok=True)
    # Step 1: package present?
    if not _importable(venv.python, "toolrecall"):
        if not _install_toolrecall(venv):
            return False
    # Step 2: drop the .pth (idempotent)
    try:
        shutil.copy2(_pth_source(), os.path.join(venv.site_packages, "tr_shim.pth"))
    except OSError:
        return False
    # Step 3: probe
    return _importable(venv.python, "toolrecall.shim")


def shim_status(venv: Venv) -> dict:
    """Report shim state for a venv without mutating anything."""
    pth = os.path.join(venv.site_packages, "tr_shim.pth")
    return {
        "root": venv.root,
        "python": venv.python,
        "site_packages": venv.site_packages,
        "package_importable": _importable(venv.python, "toolrecall"),
        "pth_present": os.path.isfile(pth),
        "probe_ok": _importable(venv.python, "toolrecall.shim"),
    }


def uninstall_shim(venv: Venv) -> bool:
    """Remove ``tr_shim.pth`` from a venv (leave the package installed)."""
    pth = os.path.join(venv.site_packages, "tr_shim.pth")
    try:
        if os.path.exists(pth):
            os.remove(pth)
        return True
    except OSError:
        return False
