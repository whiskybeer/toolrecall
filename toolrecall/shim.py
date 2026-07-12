"""
toolrecall.shim — Transparent OS-level cache shim.

Installation (one-time):
    toolrecall shim --install

This creates a .pth file in site-packages that auto-pathes
`open()`, `subprocess.run()`, and `subprocess.Popen()` on
every Python process startup. Zero imports needed from the
calling code — any Python process (Hermes, Codex, Aider,
Cursor, scripts) transparently benefits.

Uninstall:
    toolrecall shim --uninstall

Config:
    TOOLRECALL_SHIM_DISABLE=1  — disable shim at runtime
"""
import os
import builtins
import sys
import threading

_ENABLED = not os.environ.get("TOOLRECALL_SHIM_DISABLE", "")

# ─── Re-entrancy guard ───
# Prevents infinite recursion when the shim's own code path (importing
# client, connecting to daemon, reading cache DB) calls open() — which
# would be patched and call back into the shim.
# Each thread gets its own guard so concurrent Python processes are
# not blocked by each other.
_thread_local = threading.local()


def _shim_active() -> bool:
    """Check if this thread is already inside a shimmed open() call."""
    return getattr(_thread_local, "active", False)


def _enter_shim():
    """Mark thread as inside shim scope. Returns previous state."""
    prev = getattr(_thread_local, "active", False)
    _thread_local.active = True
    return prev


def _exit_shim(prev: bool):
    """Restore thread's shim-active state to what it was before entry."""
    _thread_local.active = prev


# ─── Lazy-load client on first call ───
_TR = None

def _get_tr():
    global _TR
    if _TR is None and _ENABLED:
        try:
            # Use relative import so the client module is loaded from
            # the same package directory as this shim module — not from
            # wherever sys.path resolves "toolrecall" (which can be the
            # source tree if an editable install shadows site-packages).
            from .client import (
                cached_read as cr,
                cached_terminal as ct,
            )
            _TR = {"read": cr, "terminal": ct}
        except ImportError:
            _TR = False
    return _TR


# ─── Internal infrastructure paths to skip (never benefit from caching) ───
# These are loaded from toolrecall.toml [shim].exclude_prefixes (or
# TOOLRECALL_SHIM_EXCLUDE_PREFIXES env var) on first call to _should_skip().
# Files matching these prefixes bypass the shim and go directly to the
# real open() — they are tiny, rewritten constantly, and never benefit
# from caching.  Intercepting them just pollutes the cache stats with noise.
# Empty list = bypass NOTHING (all open() calls go through the shim).
_SKIP_PREFIXES = None

def _load_skip_prefixes():
    """Load exclude prefixes from config. Call once on first use."""
    global _SKIP_PREFIXES
    if _SKIP_PREFIXES is not None:
        return
    try:
        from toolrecall.config import load_config
        cfg = load_config()
        _SKIP_PREFIXES = list(cfg.shim_exclude_prefixes)
    except Exception:
        _SKIP_PREFIXES = []

def _should_skip(path: str | bytes | os.PathLike) -> bool:
    """Check if a path is an internal infrastructure file that should bypass the shim."""
    if _SKIP_PREFIXES is None:
        _load_skip_prefixes()
    ps = os.fspath(path)
    for prefix in _SKIP_PREFIXES:
        if ps.startswith(prefix):
            return True
    return False


# ─── Patch open() ───
_original_open = builtins.open

def _shim_open(path, mode='r', *args, **kwargs):
    # Don't intercept non-file paths (integers = file descriptors,
    # None, or capture objects from test frameworks).
    if not isinstance(path, (str, bytes, os.PathLike)):
        return _original_open(path, mode, *args, **kwargs)

    path_str = os.fspath(path)

    # Re-entrancy guard: if we're already inside a shim call, fall
    # through to the real open() immediately to prevent infinite recursion.
    if _shim_active():
        return _original_open(path_str, mode, *args, **kwargs)

    prev = _enter_shim()
    try:
        # Skip Hermes internal infrastructure files — they're tiny, rewritten
        # constantly, and caching them just pollutes the stats.
        # NOTE: called inside shim scope so any open() triggered by
        # _load_skip_prefixes() is caught by the re-entrancy guard.
        if _should_skip(path_str):
            return _original_open(path_str, mode, *args, **kwargs)

        tr = _get_tr()
        if tr and 'r' in mode and 'b' not in mode:
            try:
                result = tr["read"](path_str)
                # Only serve from shim if it was a cache HIT.
                # On cache miss, fall through to _original_open so the
                # real cached_read (from cache.py) reads the file directly
                # and records stats exactly once.
                if result and result.get("cached", False) and "content" in result:
                    import io
                    return io.StringIO(result["content"])
            except Exception:
                pass
        return _original_open(path_str, mode, *args, **kwargs)
    finally:
        _exit_shim(prev)


# ─── Patch subprocess ───
try:
    import subprocess as _sp
    _original_run = _sp.run
    _original_popen = _sp.Popen
except ImportError:
    _sp = None

def _shim_run(*args, **kwargs):
    tr = _get_tr()
    if tr and args:
        cmd = args[0] if args else kwargs.get("args", "")
        # Only route string commands through cached_terminal.
        # List-form commands (e.g. ["python3", "-c", code]) are passed
        # through to the original subprocess.run — cached_terminal expects
        # a shell string and shlex.split would mangle quoting in code strings.
        if isinstance(cmd, str):
            try:
                result = tr["terminal"](cmd)
                if result and "output" in result and "exit_code" in result:
                    from subprocess import CompletedProcess
                    return CompletedProcess(
                        args=args[0] if args else kwargs.get("args", []),
                        returncode=result["exit_code"],
                        stdout=result["output"],
                        stderr=result.get("error", ""),
                    )
            except Exception:
                pass
    return _original_run(*args, **kwargs)

# Popen stays original (background/captured output can't cache)
_shim_popen = _original_popen if _sp else None


def apply():
    """Apply all shim monkey-patches. Called once on .pth import.

    Skips patching when running under pytest (interferes with capture)
    unless force=True is passed (for tests that explicitly test the shim).
    """
    if not _ENABLED:
        return
    # Don't patch when running under pytest — interferes with stdout/stderr capture.
    # At .pth load time, pytest isn't in sys.modules yet. Detection:
    # - pytest binary: sys.argv[0] contains 'pytest' (e.g. /usr/local/bin/pytest)
    # - python3 -m pytest: sys.argv[0] is '-m' (module mode — can't tell which module)
    # - PYTEST_CURRENT_TEST env var is set during test execution
    # For the '-m' case, we can't distinguish pytest from other modules at .pth time,
    # so we also check if any arg looks like a test path.
    _argv = sys.argv[:5] if sys.argv else []
    if any("pytest" in str(a) for a in _argv):
        return
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return
    # For 'python3 -m pytest tests/...' — check for test-like args
    if _argv and _argv[0] == "-m" and any(
        str(a).startswith("test") or "pytest" in str(a)
        for a in _argv[1:]
    ):
        return
    builtins.open = _shim_open
    if _sp:
        _sp.run = _shim_run
        _sp.Popen = _shim_popen


def remove():
    """Restore all original functions."""
    builtins.open = _original_open
    if _sp:
        _sp.run = _original_run
        _sp.Popen = _original_popen


# ─── Auto-apply on .pth import ───
apply()