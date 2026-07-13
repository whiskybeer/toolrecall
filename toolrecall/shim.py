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
        if tr and mode in ('r', 'rt'):
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
import re
import shlex
import subprocess as _sp
_original_run = _sp.run
_original_popen = _sp.Popen

# Shell metacharacters that indicate a string command is not a simple
# single-word call — routing it through cached_terminal would mangle it.
_SHELL_METACHARS = re.compile(r'[|;&><$`*?()\[\]{}#!~^]')


def _is_safe_string_command(cmd: str, kwargs: dict) -> bool:
    """Check if a string command is safe to route through cached_terminal.

    Safe = the caller wants captured output (capture_output=True or
    stdout=PIPE, text=True) so we can return a CompletedProcess with
    stdout/stderr, AND no kwargs that cached_terminal can't preserve.

    Calls without capture (e.g. shell=True with no capture) expect
    output on the console and stdout=None — routing them through
    cached_terminal would silently change semantics.
    """
    import subprocess

    # Must have capturing enabled — otherwise the caller expects
    # console output and stdout=None, not a str.
    capture = kwargs.get("capture_output", False)
    stdout = kwargs.get("stdout", None)
    if not capture and stdout is not subprocess.PIPE:
        return False

    # Must have text=True or universal_newlines=True — cached_terminal returns str, not bytes
    if not kwargs.get("text", False) and not kwargs.get("universal_newlines", False):
        return False

    # Kwargs that cached_terminal can't preserve
    if any(k in kwargs for k in ('cwd', 'env', 'input', 'check')):
        return False

    # Shell metacharacters would be mangled by shlex.split
    if _SHELL_METACHARS.search(cmd):
        return False
    return True


def _shim_run(*args, **kwargs):
    tr = _get_tr()
    if tr and args:
        cmd = args[0] if args else kwargs.get("args", "")
        if isinstance(cmd, str) and _is_safe_string_command(cmd, kwargs):
            try:
                result = tr["terminal"](cmd)
                if result and "output" in result and "exit_code" in result:
                    from subprocess import CompletedProcess

                    stdout = result.get("output", "")
                    stderr = result.get("error", result.get("stderr", ""))
                    return CompletedProcess(
                        args=args[0] if args else kwargs.get("args", []),
                        returncode=result["exit_code"],
                        stdout=stdout,
                        stderr=stderr,
                    )
            except Exception:
                pass
    return _original_run(*args, **kwargs)


def apply():
    """Apply all shim monkey-patches. Called once on .pth import.

    Skips patching when running under pytest (interferes with capture)
    unless force=True is passed (for tests that explicitly test the shim).
    """
    if not _ENABLED:
        return
    # Don't patch when running under pytest — interferes with stdout/stderr capture.
    # At .pth load time, pytest isn't in sys.modules yet. Detection:
    # - pytest binary: sys.argv[0] basename starts with 'pytest'
    # - python3 -m pytest: sys.argv[0] is '-m' (module mode — can't tell which module)
    # - PYTEST_CURRENT_TEST env var is set during test execution
    _argv = sys.argv[:5] if sys.argv else []
    if any(os.path.basename(str(a)).startswith("pytest") for a in _argv[:1]):
        return
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return
    # For 'python3 -m pytest tests/...' — check for pytest in remaining args
    if _argv and _argv[0] == "-m" and any(
        "pytest" in str(a)
        for a in _argv[1:]
    ):
        return
    builtins.open = _shim_open
    if _sp:
        _sp.run = _shim_run


def remove():
    """Restore all original functions."""
    builtins.open = _original_open
    if _sp:
        _sp.run = _original_run


# ─── Auto-apply on .pth import ───
apply()