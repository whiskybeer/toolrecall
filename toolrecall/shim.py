"""
toolrecall.shim — Transparent OS-level file-read cache shim.

Installation (one-time):
    toolrecall shim --install

This creates a .pth file in site-packages that auto-patches `open()`
(read-only) on every Python process startup. Zero imports needed from the
calling code — any Python process (Hermes, Codex, Aider, Cursor, scripts)
transparently benefits from file-read caching.

Scope (Option B — see CHANGELOG v0.8.19):
    Patches ONLY `builtins.open` for read (`r`/`rt`) mode. It does NOT
    intercept `subprocess.run()`/`subprocess.Popen()`. Terminal command
    output is NOT transparently cached at the shim layer: doing so is
    fundamentally lossy because it runs the command in the daemon's working
    directory and environment rather than the calling agent's, then replays
    the (wrong) output. That wrong-cwd class of bug surfaced as garbled
    shell output and offline-looking sessions. Terminal/output caching that
    is known to be read-only and deterministic stays available explicitly
    via the daemon (`cached_terminal`), never through the implicit shim.

File-read caching is safe to shim because it is keyed on path + mtime +
size — a read is deterministic given the same bytes, so a cache hit can
never return wrong content.

Uninstall:
    toolrecall shim --uninstall

Config:
    TOOLRECALL_SHIM_DISABLE=1  — disable shim at runtime
    toolrecall shim --disable  — persistent disable (writes ~/.toolrecall/shim.disabled)
    toolrecall shim --enable   — clear the persistent disable marker
"""

import os
import builtins
import sys
import threading


def _marker_path() -> str:
    """Path of the persistent disable marker.

    Honors ``TOOLRECALL_SHIM_MARKER`` to point at a custom location (useful for
    tests and multi-daemon setups). Defaults to ~/.toolrecall/shim.disabled.
    """
    override = os.environ.get("TOOLRECALL_SHIM_MARKER", "")
    if override:
        return override
    return os.path.join(os.path.expanduser("~/.toolrecall"), "shim.disabled")


def _marker_disabled() -> bool:
    """True if a persistent disable marker file exists."""
    p = _marker_path()
    if not p:
        return False
    return os.path.isfile(p)


_ENABLED = not (os.environ.get("TOOLRECALL_SHIM_DISABLE", "") or _marker_disabled())

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
            from .client import cached_read as cr

            _TR = {"read": cr}
        except ImportError:
            _TR = False
    return _TR


# ─── Internal infrastructure paths to skip (never benefit from caching) ───
# These are loaded from toolrecall.toml [shim].exclude_prefixes (or
# TOOLRECALL_SHIM_EXCLUDE_PREFIXES env var) on first call to _should_skip().
# Files matching these prefixes bypass the shim and go directly to the
# real open() — they are tiny, rewritten constantly, and never benefit
# from caching. Intercepting them just pollutes the cache stats with noise.
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
    assert _SKIP_PREFIXES is not None
    ps = os.fspath(path)
    for prefix in _SKIP_PREFIXES:
        if ps.startswith(prefix):
            return True
    return False


# ─── Patch open() ───
_original_open = builtins.open


def _open_cached_content(content, mode="r", *args, **kwargs):
    """Return a *real* file object backed by the cached ``content`` str.

    The shim must hand back something that behaves like a normal
    ``open(path, *args, **kwargs)`` result: consumers legitimately rely on
    ``.fileno()``, ``.buffer``, ``.name``, ``os.fstat()``, mmap, select, or
    piping the handle to a subprocess. A plain ``io.StringIO`` provides none
    of that and crashes terminal/bash-style callers, so on a cache hit we
    materialize the cached bytes into a temporary file and return a real
    handle. The temp file is unlinked immediately after open; on POSIX the
    inode stays alive until the returned handle is closed.

    Any exception (disk full, encoding error, ...) propagates so the caller's
    ``except`` falls back to reading the original path directly.
    """
    import tempfile

    fd = None
    tmp_path = None
    try:
        fd, tmp_path = tempfile.mkstemp(prefix=".toolrecall-shim-", suffix=".tmp")
        # Persist cached bytes with the same encoding the re-opened handle
        # will use, so the round-trip is lossless for any encoding.
        enc = kwargs.get("encoding") or "utf-8"
        with os.fdopen(fd, "w", encoding=enc) as f:
            f.write(content)
        fd = None  # ownership moved into the with-block
        handle = _original_open(tmp_path, mode, *args, **kwargs)
        try:
            os.unlink(tmp_path)  # POSIX: inode lives until handle is closed
        except OSError:
            pass
        return handle
    except Exception:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        raise


def _shim_open(path, mode="r", *args, **kwargs):
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
        if tr and mode in ("r", "rt"):
            try:
                result = tr["read"](path_str, source="shim")
                # Only serve from shim if it was a cache HIT.
                # On cache miss, fall through to _original_open so the
                # real cached_read (from cache.py) reads the file directly
                # and records stats exactly once.
                if result and result.get("cached", False) and "content" in result:
                    return _open_cached_content(result["content"], mode, *args, **kwargs)
            except Exception:
                pass
        return _original_open(path_str, mode, *args, **kwargs)
    finally:
        _exit_shim(prev)


def apply():
    """Apply the shim monkey-patch. Called once on .pth import.

    Patches ONLY builtins.open for read-mode caching. Intentionally does
    NOT touch subprocess.run/Popen — see module docstring (Option B).
    Skips patching when running under pytest (interferes with capture).
    """
    if not _ENABLED:
        return
    # Don't patch when running under pytest.
    _argv = sys.argv[:5] if sys.argv else []
    if any(os.path.basename(str(a)).startswith("pytest") for a in _argv[:1]):
        return
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return
    if _argv and _argv[0] == "-m" and any("pytest" in str(a) for a in _argv[1:]):
        return
    builtins.open = _shim_open


def remove():
    """Restore the original builtins.open."""
    builtins.open = _original_open


def disable() -> bool:
    """Persistently disable the shim by writing the marker file.

    Returns True on success. Existing processes keep running as-is; new
    Python processes will skip the shim (marker is honored at import).
    """
    try:
        p = _marker_path()
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write("disabled")
        return True
    except OSError:
        return False


def enable() -> bool:
    """Re-enable the shim by removing the persistent disable marker."""
    try:
        p = _marker_path()
        if p and os.path.exists(p):
            os.remove(p)
        return True
    except OSError:
        return False


# ─── Auto-apply on .pth import ───
apply()
