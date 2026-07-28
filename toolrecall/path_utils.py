"""Shared path validation utilities for ToolRecall.

Extracted to avoid duplicating allowlist logic across daemon and client fallback.
Both daemon.py:SecurityGate.check_read_path() and client.py:cached_read() daemon-unavailable
fallback now call the shared helper.

Includes Windows case-insensitive path comparison via os.path.normcase().
"""

import os


def check_path_allowed(path: str, allowed_paths: list) -> bool:
    """Check if *path* falls within any of *allowed_paths*.

    Rules:
    1. Empty allowed_paths → False (default-deny / fail-closed)
    2. Expands ``~`` and resolves symlinks via ``realpath``
    3. Applies ``normcase()`` for Windows case-insensitive comparison
    4. Prefix-boundary guard: ``startswith(allowed_abs + os.sep)`` prevents
       ``/data-evil`` from matching ``/data``
    5. Exact match also accepted (path == allowed_abs)

    Returns:
        True if path is allowed, False if denied.
    """
    if not allowed_paths:
        # Empty list = nothing is allowed (fail-closed / default-deny)
        return False

    path = os.path.expanduser(path)
    abs_path = os.path.realpath(path)
    abs_path_norm = os.path.normcase(abs_path)

    for allowed in allowed_paths:
        allowed = os.path.expanduser(allowed)
        allowed_abs = os.path.realpath(allowed)
        allowed_norm = os.path.normcase(allowed_abs)
        if abs_path_norm == allowed_norm or abs_path_norm.startswith(allowed_norm + os.sep):
            return True

    # Symlink escape check: if the original path (before realpath) lives
    # inside an allowed directory, permit it.  A user who puts a symlink
    # inside an allowed path has intentionally made that content available.
    # (e.g. /etc/os-release -> /usr/lib/os-release, /etc is in allowed_paths)
    expanded_original = os.path.expanduser(path)
    abs_original = os.path.abspath(expanded_original)
    original_norm = os.path.normcase(abs_original)
    for allowed in allowed_paths:
        allowed_abs = os.path.realpath(os.path.expanduser(allowed))
        allowed_norm = os.path.normcase(allowed_abs)
        if original_norm == allowed_norm or original_norm.startswith(allowed_norm + os.sep):
            return True

    return False
