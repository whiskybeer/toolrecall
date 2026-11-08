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
import sys
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
    # Never return the interpreter we are running as: shimming ourselves is
    # pointless, and because entries are keyed on the interpreter realpath it
    # would also stand in for every other venv sharing that interpreter.
    # This matters for uv-managed pythons — a uv venv's ``python`` is a symlink
    # to a SHARED base interpreter under ~/.local/share/uv, so it does not live
    # "under the repo" and slips past _is_toolrecall_own(). (uv is what
    # ``make sync`` uses, so this is the normal worktree setup, not an edge case.)
    own_interpreter = _real(sys.executable)

    for py in raw:
        py = _real(py)
        if not py or py in seen or py == own_interpreter or _is_toolrecall_own(py):
            continue
        seen.add(py)
        sp = _site_packages_of(py)
        if not sp:
            continue  # not truly a probeable venv / toolrecall-free env
        root = os.path.dirname(os.path.dirname(py))
        out.append(Venv(root=root, python=py, site_packages=sp))
    return out


def _probe_env() -> dict:
    """Env for probe subprocesses, stripped of pytest-only markers.

    ``PYTEST_CURRENT_TEST`` is inherited by children; ``shim.apply()`` treats
    it as "running under pytest" and refuses to patch, which made every
    real-venv test read as "shim not applied" (2026-11-07). A probe must
    answer for a normal interpreter, not for the test harness.
    """
    env = dict(os.environ)
    for key in ("PYTEST_CURRENT_TEST", "PYTEST_ADDOPTS"):
        env.pop(key, None)
    return env


def _shim_applied(python: str) -> bool:
    """True when a fresh interpreter has the shim ACTUALLY hooked into open().

    ``_importable`` only proves the module exists; a .pth that runs before the
    package is on sys.path imports fine in a later probe yet never applies the
    hook at startup (the 2026-11-07 zz_-ordering bug: probe said pass, every
    real process printed "Error processing line 1" and ran unshimmed). Only
    checking ``builtins.open`` catches that class of false "verified".
    """
    try:
        with tempfile.TemporaryDirectory(prefix="tr-probe-") as td:
            r = subprocess.run(
                [python, "-c", "import builtins;print(getattr(builtins.open,'__name__',''))"],
                capture_output=True,
                text=True,
                timeout=20,
                cwd=td,
                env=_probe_env(),
            )
        return r.returncode == 0 and "_shim_open" in r.stdout
    except (OSError, subprocess.TimeoutExpired):
        return False


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
                env=_probe_env(),
            )
        return r.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


#: Modules whose bytes define shim behaviour. Two copies that disagree here run
#: different shim logic no matter what the version metadata says: an
#: editable/overlay install keeps whatever dist-info version it was first
#: installed with, and a repo runs ahead of ``__version__`` between releases.
#: Version strings therefore cannot see dev-ahead drift — content can.
_SHIM_MANIFEST_FILES = (
    "shim.py",
    "shim_stub.py",
    "stub_core.py",
    "client.py",
    "keys.py",
    "config.py",
)

#: Overlay .pth name — the file ``scripts/repo_overlay.py`` writes for the pipx
#: venv. A bare source-tree path on a .pth line makes that env import the live
#: tree, so a converged venv cannot drift again.
#: Must sort BEFORE ``tr_shim.pth`` so the package is importable when the shim
#: line runs at interpreter startup. ``zz_`` sorted last, so every fresh venv
#: printed "Error processing line 1 of tr_shim.pth: No module named
#: 'toolrecall'" and silently ran unshimmed (found 2026-11-07).
_OVERLAY_PTH = "00_toolrecall_repo.pth"
_LEGACY_OVERLAY_PTH = "zz_toolrecall_repo.pth"


def _manifest_of(pkg_dir: str) -> Optional[str]:
    """Content fingerprint of the shim modules in ``pkg_dir`` (None if absent).

    Name + bytes of every file in ``_SHIM_MANIFEST_FILES``, in sorted order, in
    one ``content_digest``. Missing files are marked explicitly, so a
    half-removed copy cannot fingerprint the same as a complete one.
    """
    from toolrecall.keys import content_digest

    parts: List[bytes] = []
    found = False
    for name in sorted(_SHIM_MANIFEST_FILES):
        parts.append(name.encode("utf-8"))
        try:
            with open(os.path.join(pkg_dir, name), "rb") as fh:
                parts.append(fh.read())
            found = True
        except OSError:
            parts.append(b"<missing>")
        parts.append(b"\0")
    return content_digest(b"".join(parts)) if found else None


def _running_manifest() -> Optional[str]:
    """Fingerprint of the shim modules backing THIS install (None if unreadable)."""
    return _manifest_of(os.path.dirname(os.path.abspath(__file__)))


def _venv_manifest(python: str) -> Optional[str]:
    """Fingerprint of the shim modules inside ``python``'s env.

    Empty/None means the env holds no shim files at all (package absent or
    half-removed). The bytes are hashed by the target interpreter with plain
    ``hashlib`` over the exact layout ``_manifest_of`` produces — deliberately
    NOT by importing the target's own ``keys`` module, because the copy being
    audited is precisely the one that may be broken or stale.
    """
    code = (
        "import hashlib, os\n"
        "try:\n"
        "    import toolrecall\n"
        "except Exception:\n"
        "    raise SystemExit(0)\n"
        "roots = [p for p in (getattr(toolrecall, '__path__', None) or []) if p]\n"
        "if not roots:\n"
        "    raise SystemExit(0)\n"
        "parts, found = [], False\n"
        f"for name in sorted({_SHIM_MANIFEST_FILES!r}):\n"
        "    parts.append(name.encode('utf-8'))\n"
        "    try:\n"
        "        with open(os.path.join(roots[0], name), 'rb') as fh:\n"
        "            parts.append(fh.read())\n"
        "        found = True\n"
        "    except OSError:\n"
        "        parts.append(b'<missing>')\n"
        "    parts.append(b'\\0')\n"
        "if found:\n"
        "    print(hashlib.sha256(b''.join(parts)).hexdigest())\n"
    )
    try:
        with tempfile.TemporaryDirectory(prefix="tr-manifest-") as td:
            r = subprocess.run(
                [python, "-c", code],
                capture_output=True,
                text=True,
                timeout=20,
                cwd=td,
            )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    out = r.stdout.strip()
    return out or None


def _source_root() -> Optional[str]:
    """The checkout backing THIS install when it is a source tree (else None).

    True for an editable install and for the repo-overlay .pth: the package dir
    then sits inside a project dir carrying ``pyproject.toml``. A drifted
    shimmed venv is converged onto that tree rather than onto a PyPI release,
    which may not exist yet or may be older than the running code.
    """
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    try:
        with open(os.path.join(root, "pyproject.toml"), "rb") as fh:
            head = fh.read(8192)
    except OSError:
        return None
    if b'name = "toolrecall"' not in head:
        return None
    return root


def _overlay_into(venv: Venv, source_root: str) -> bool:
    """Make ``venv`` import ``source_root`` via the repo-overlay .pth.

    Mirrors ``scripts/repo_overlay.py``: the stale snapshot directory is
    removed first, because a plain .pth path is *appended* to sys.path and a
    leftover ``site-packages/toolrecall`` would keep shadowing the live tree.
    Reversible by deleting the .pth.
    """
    try:
        os.makedirs(venv.site_packages, exist_ok=True)
        stale = os.path.join(venv.site_packages, "toolrecall")
        if os.path.isdir(stale):
            shutil.rmtree(stale)
        # Retire the legacy zz_-named pth: it sorted after tr_shim.pth, so the
        # shim ran before the repo path existed and always failed to import.
        legacy = os.path.join(venv.site_packages, _LEGACY_OVERLAY_PTH)
        if os.path.exists(legacy):
            os.remove(legacy)
        with open(os.path.join(venv.site_packages, _OVERLAY_PTH), "w") as fh:
            fh.write(source_root + "\n")
        return True
    except OSError:
        return False


def _running_version() -> Optional[str]:
    """Version of the toolrecall package backing THIS code (None if unknown).

    The running install is authoritative for shim targets: when toolrecall
    itself was upgraded (pipx / repo .venv / uv tool), previously-shimmed
    agent venvs still hold the old copy, so ``ensure_shim`` compares against
    this version instead of only checking presence.
    """
    try:
        from importlib.metadata import version

        return version("toolrecall")
    except Exception:
        return None


def _installed_version(python: str) -> Optional[str]:
    """Version of ``toolrecall`` inside ``python``'s env (None if absent)."""
    code = "from importlib.metadata import version; print(version('toolrecall'))"
    try:
        with tempfile.TemporaryDirectory(prefix="tr-venvver-") as td:
            r = subprocess.run(
                [python, "-c", code],
                capture_output=True,
                text=True,
                timeout=20,
                cwd=td,  # neutral cwd: repo/agent dirs on sys.path would lie
            )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode == 0:
        v = r.stdout.strip()
        return v or None
    return None


def _install_toolrecall(venv: Venv) -> bool:
    """Install/upgrade the ``toolrecall`` package into the target venv.

    Prefers ``pip`` present in the venv; falls back to ``uv pip install`` with
    ``--python`` if pip is absent but uv is available. Read-only/cached uv
    venvs that can't be written are caught and reported False.

    ``--upgrade`` is passed explicitly: a bare ``pip install`` is a no-op
    ("Requirement already satisfied") when an older copy is present, which is
    exactly the drift this module exists to prevent.
    """
    pip = os.path.join(os.path.dirname(venv.python), "pip")
    if os.access(pip, os.X_OK):
        try:
            r = subprocess.run(
                [pip, "install", "--upgrade", "toolrecall"],
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
                [uv, "pip", "install", "--python", venv.python, "--upgrade", "toolrecall"],
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


def _migrate_overlay_pth(venv: Venv) -> None:
    """Rewrite a legacy ``zz_`` repo overlay to the startup-safe name.

    A leftover zz_ file makes the fingerprint probe (which imports toolrecall
    later in startup) see "in sync" while the real interpreter still fails at
    ``tr_shim.pth`` — so drift detection must not be the only trigger for the
    rename.
    """
    legacy = os.path.join(venv.site_packages, _LEGACY_OVERLAY_PTH)
    if not os.path.isfile(legacy):
        return
    try:
        with open(legacy) as fh:
            target = fh.read().strip()
    except OSError:
        return
    try:
        if target:
            with open(os.path.join(venv.site_packages, _OVERLAY_PTH), "w") as fh:
                fh.write(target + "\n")
        os.remove(legacy)
    except OSError:
        pass


def ensure_shim(venv: Venv) -> bool:
    """Install the shim into ``venv`` idempotently and verify by probe + manifest.

    Steps:
      1. Ensure the shim modules in the venv MATCH the running install,
         compared by content fingerprint (``_SHIM_MANIFEST_FILES``) instead of
         by version string. Version comparison passes whenever the metadata
         agrees while the code differs — a hand-synced snapshot, an editable
         install whose dist-info lags its tree, or a repo running ahead of an
         unchanged ``__version__`` — which is how shimmed agent venvs silently
         kept pre-fix shim behaviour. On drift: upgrade from the index, then —
         when the running install is a source tree — overlay the venv onto it.
      2. Copy ``tr_shim.pth`` into ``venv.site_packages``.
      3. Probe ``import toolrecall.shim`` from a neutral cwd and re-read the
         fingerprint.

    Returns True only when the probe passes AND the fingerprint now matches, so
    a venv that is still behind reports a failure instead of a false "verified".
    """
    os.makedirs(venv.site_packages, exist_ok=True)
    _migrate_overlay_pth(venv)
    # Step 1: content identity, not version identity.
    running = _running_manifest()
    target = _venv_manifest(venv.python)
    if target is None or (running is not None and target != running):
        # Index first: it is the release-tracked path and a no-op when the
        # index has nothing newer (which is exactly the dev-ahead case).
        _install_toolrecall(venv)
        target = _venv_manifest(venv.python)
        if running is not None and target != running:
            source = _source_root()
            if source is not None:
                _overlay_into(venv, source)
                target = _venv_manifest(venv.python)
    # Step 2: drop the .pth (idempotent)
    try:
        shutil.copy2(_pth_source(), os.path.join(venv.site_packages, "tr_shim.pth"))
    except OSError:
        return False
    # Step 3: probe + fingerprint check
    if not _importable(venv.python, "toolrecall.shim"):
        return False
    if not _shim_applied(venv.python):
        return False
    if running is None or target in (None, running):
        return True
    return _venv_manifest(venv.python) == running


def shim_status(venv: Venv) -> dict:
    """Report shim state for a venv without mutating anything.

    ``drift`` is the honest health bit: the venv's shim modules differ in
    *content* from the running install, so it runs other (older) shim logic
    even when ``version_venv == version_source``. Presence + import probes
    cannot see that — only the fingerprint can.
    """
    pth = os.path.join(venv.site_packages, "tr_shim.pth")
    source_manifest = _running_manifest()
    venv_manifest = _venv_manifest(venv.python)
    return {
        "root": venv.root,
        "python": venv.python,
        "site_packages": venv.site_packages,
        "package_importable": _importable(venv.python, "toolrecall"),
        "pth_present": os.path.isfile(pth),
        "probe_ok": _importable(venv.python, "toolrecall.shim"),
        "applied": _shim_applied(venv.python),
        "version_source": _running_version(),
        "version_venv": _installed_version(venv.python),
        "manifest_source": source_manifest,
        "manifest_venv": venv_manifest,
        "drift": source_manifest is not None and venv_manifest != source_manifest,
    }


def uninstall_shim(venv: Venv) -> bool:
    """Remove ``tr_shim.pth`` (and any repo overlay) from a venv.

    Leaves the package installed; only the auto-import hooks go.
    """
    try:
        for name in ("tr_shim.pth", _OVERLAY_PTH, _LEGACY_OVERLAY_PTH):
            pth = os.path.join(venv.site_packages, name)
            if os.path.exists(pth):
                os.remove(pth)
        return True
    except OSError:
        return False
