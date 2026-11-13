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

import io
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from typing import List, Optional

#: Paths under $HOME known to host agent venvs. Scanned in addition to the
#: generic ``*/bin/python3* + pyvenv.cfg`` walk. Be generous here; each hit is
#: deduped by venv root and validated by asking the interpreter itself.
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


def _venv_root_of(path: str, max_up: int = 3) -> Optional[str]:
    """Return the venv root containing ``path``, per PEP 405.

    Identity is the directory carrying ``pyvenv.cfg`` — not the interpreter
    binary, which may be a symlink (or a hardlink/copy) shared by any number of
    environments. ``path`` may be a launcher, an interpreter, or a root.
    Returns the resolved root, or None when the path is a plain interpreter.
    """
    p = os.path.abspath(path)
    if not os.path.isdir(p):
        p = os.path.dirname(p)
    for _ in range(max_up + 1):
        if os.path.isfile(os.path.join(p, "pyvenv.cfg")):
            return _real(p)
        parent = os.path.dirname(p)
        if parent == p:
            break
        p = parent
    return None


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
    """Interpreter hints from live Python processes (Linux; [] elsewhere).

    ``/proc/<pid>/exe`` resolves through a venv's ``bin/python3`` symlink to the
    BASE interpreter and loses which venv the process belongs to. The absolute
    launch path in ``argv[0]`` keeps it. Both are returned; discovery keys on
    the venv root, so duplicates are harmless.

    ``/proc`` is read with ``os.open`` (not ``open``): this runs inside a
    shimmed CLI process on some machines, and process state must never be
    served from a file cache.
    """
    pythons: List[str] = []
    try:
        entries = os.listdir("/proc")
    except (FileNotFoundError, PermissionError):
        return pythons
    for entry in entries:
        if not entry.isdigit():
            continue
        try:
            exe = os.readlink(f"/proc/{entry}/exe")
        except OSError:
            continue
        if not os.path.basename(exe).startswith("python"):
            continue
        pythons.append(exe)
        try:
            fd = os.open(f"/proc/{entry}/cmdline", os.O_RDONLY)
            try:
                argv0 = os.read(fd, 4096).split(b"\0", 1)[0].decode("utf-8", "replace")
            finally:
                os.close(fd)
        except OSError:
            continue
        if (
            os.path.isabs(argv0)
            and os.path.basename(argv0).startswith("python")
            and os.path.isfile(argv0)
        ):
            pythons.append(argv0)
    return pythons


def _candidate_pythons_from_home(include_generic: bool = True) -> List[str]:
    """Walk $HOME for venv launcher scripts (agent roots, optionally generic).

    A proper venv is identified by a ``pyvenv.cfg`` at its root. For each such
    cfg we resolve the launcher script ``<root>/bin/python3`` — this avoids
    any ambiguity about whether ``bin/`` etc. are traversed.

    ``include_generic`` adds the depth-3 $HOME sweep (``~/proj/.venv``). It is
    used only when nothing is running and no agent root exists: project venvs
    nobody runs an agent in are not shim targets, and sweeping them would put
    the shim into benchmark environments.
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
    if include_generic:
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

    Sources (deduped by venv root):
      1. Live ``python`` processes (``/proc/<pid>/exe`` + absolute ``argv[0]``).
      2. ``$HOME`` walk for agent-root venvs + generic ``*/bin/python3*`` w/
         an adjacent ``pyvenv.cfg``.
    Always excludes toolrecall's own environment(s).

    Two environments may share one interpreter binary — every uv/PM venv links
    its ``bin/python3`` to a shared base CPython — so identity is the venv root
    (PEP 405 ``pyvenv.cfg``), not the resolved interpreter. Their site-packages,
    and therefore their shim, are separate targets.
    """
    seen: set[str] = set()
    out: List[Venv] = []

    raw = _python_from_running_procs() + _candidate_pythons_from_home(include_generic=False)
    if not raw:
        # Nothing running and no agent root on this machine: fall back to a
        # plain $HOME sweep so a single project venv still works out of the box.
        raw = _candidate_pythons_from_home(include_generic=True)
    source_root = _real(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    own_roots = {_real(sys.prefix)}
    repo_venv = os.path.join(source_root, ".venv")
    if os.path.isdir(repo_venv):
        own_roots.add(_real(repo_venv))

    for py in raw:
        if not os.path.isfile(py) or not os.access(py, os.X_OK):
            continue
        root = _venv_root_of(py)
        if root is None:
            root = _real(os.path.dirname(os.path.dirname(os.path.abspath(py))))
        if root in own_roots or _is_toolrecall_own(py):
            continue
        if set(root.split(os.sep)) & _SKIP_DIRNAMES:
            # Disposable environments (uv archives, caches, VCS trees) are not
            # runtimes an agent persists in; probing them only adds noise.
            continue
        if root in seen:
            continue
        seen.add(root)
        sp = _site_packages_of(py)
        if not sp:
            continue  # not truly a probeable venv / toolrecall-free env
        out.append(Venv(root=root, python=py, site_packages=sp))
    return out


def required_venv_targets(patterns: List[str]) -> List[Venv]:
    """Resolve ``[shim].required_venvs`` patterns to concrete venvs (deduped).

    Patterns are fnmatch-style globs over absolute paths (``~`` expanded) and
    may match a venv root or its ``bin/python*``. Matching the filesystem
    directly — rather than only discovered venvs — keeps this usable from an
    update hook for an environment whose process has not started yet.
    Unmatched patterns are the caller's business: they are reported, not raised.
    """
    import glob as _glob

    roots: List[str] = []
    for pat in patterns:
        for hit in _glob.glob(os.path.expanduser(pat)):
            root = _venv_root_of(hit)
            if root is None and os.path.isdir(hit):
                # Non-PEP-405 environments (e.g. a uv-managed interpreter that
                # carries its own site-packages but no pyvenv.cfg) are still
                # valid shim targets; the site-packages probe decides below.
                root = _real(hit)
            if root and root not in roots:
                roots.append(root)

    out: List[Venv] = []
    for root in sorted(roots):
        bindir = os.path.join(root, "bin")
        if not os.path.isdir(bindir):
            continue
        py = None
        for name in sorted(os.listdir(bindir)):
            if name.startswith("python3"):
                full = os.path.join(bindir, name)
                if os.access(full, os.X_OK):
                    py = full
                    break
        if not py:
            continue
        sp = _site_packages_of(py)
        if not sp:
            continue
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


def _read_bytes(path: str) -> bytes:
    """Read a file without ``builtins.open``.

    The transparent shim (and any similar hook) patches ``builtins.open`` to
    serve cache content. A fingerprint of the installed shim modules must
    describe the bytes on disk, so it reads through ``io.FileIO`` — the C-level
    opener that no file-cache hook substitutes.
    """
    with io.FileIO(path, "rb") as fh:
        return fh.read()


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
            parts.append(_read_bytes(os.path.join(pkg_dir, name)))
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
    half-removed). The target interpreter is the only reliable oracle for
    *which copy* it imports — repo overlays and editable installs make the
    site-packages path the wrong answer — so it prints that directory. The
    bytes are then read here, from disk, through ``_read_bytes``: never through
    a file-cache hook that may be active in the child or in this process.
    """
    code = (
        "import toolrecall\n"
        "roots = [p for p in (getattr(toolrecall, '__path__', None) or []) if p]\n"
        "if not roots:\n"
        "    raise SystemExit(0)\n"
        "print(roots[0])\n"
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
    lines = [ln.strip() for ln in r.stdout.splitlines() if ln.strip()]
    if not lines:
        return None
    pkg_dir = lines[-1]
    if not os.path.isdir(pkg_dir):
        return None
    return _manifest_of(pkg_dir)


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

    An absent copy (``manifest_venv is None``) is *not* drift: there is nothing
    to converge, the package simply is not installed in that venv.
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
        "drift": (
            source_manifest is not None
            and venv_manifest is not None
            and venv_manifest != source_manifest
        ),
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
