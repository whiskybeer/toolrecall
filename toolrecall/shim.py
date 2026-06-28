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
import sys
import builtins

_ENABLED = not os.environ.get("TOOLRECALL_SHIM_DISABLE", "")

# ─── Lazy-load client on first call ───
_TR = None

def _get_tr():
    global _TR
    if _TR is None and _ENABLED:
        try:
            from toolrecall.client import (
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
        if isinstance(cmd, list):
            cmd = " ".join(str(c) for c in cmd)
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