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


# ─── Patch open() ───
_original_open = builtins.open

def _shim_open(path, mode='r', *args, **kwargs):
    # Re-entrancy guard: if we're already inside a shim call (e.g. the
    # daemon's open(), or importing client triggers another open()), fall
    # through to the real open() immediately to prevent infinite recursion.
    if _shim_active():
        return _original_open(path, mode, *args, **kwargs)

    prev = _enter_shim()
    try:
        tr = _get_tr()
        if tr and 'r' in mode and 'b' not in mode:
            try:
                result = tr["read"](os.fspath(path))
                if result and "content" in result:
                    import io
                    return io.StringIO(result["content"])
            except Exception:
                pass
        return _original_open(path, mode, *args, **kwargs)
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
    """Apply all shim monkey-patches. Called once on .pth import."""
    if not _ENABLED:
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