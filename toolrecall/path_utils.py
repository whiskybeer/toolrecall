"""Shared path validation utilities for ToolRecall.

Extracted to avoid duplicating allowlist logic across daemon and client fallback.
Both daemon.py:SecurityGate.check_read_path() and client.py:cached_read() daemon-unavailable
fallback now call the shared helper.

Includes Windows case-insensitive path comparison via os.path.normcase().

Symlink policy (F1) — explicit, configurable, documented:
  Layer A (always): the REALPATH of the requested path must sit inside an
    allowed directory. This is the containment boundary: `ln -s /etc ~/proj/x`
    never grants access to /etc.
  Layer B (opt-in): ADDITIONALLY, the un-resolved (lexical) path may itself
    sit inside an allowed directory even when its target does not. Default
    OFF (`[security] allow_symlink_escapes = false`, review 2026-09: the
    attack against Layer B is easiest on Windows, so a stock install is
    strict). Legitimate layouts that depend on the rescue — /etc/os-release
    → /usr/lib/os-release, venv/site-packages links, distro-managed
    symlinks — opt in with `allow_symlink_escapes = true`. A user who
    places a symlink inside an allowed directory has, by that act, shared
    its target.

  Known residual risk with Layer B on (opt-in): a WRITER inside an allowed
  directory (agent with write access, or a sibling process) can create a
  symlink to anywhere and have it read through the gate. Mitigations that
  remain active regardless: the sensitive-file blocklist (daemon Layer 2)
  still applies, and ToolRecall's MCP tools are read-only by default.
"""

import os


def check_path_allowed(path: str, allowed_paths: list, allow_symlink_escapes: bool = False) -> bool:
    """Check if *path* falls within any of *allowed_paths*.

    Rules:
    1. Empty allowed_paths → False (default-deny / fail-closed)
    2. Expands ``~``; resolution is two-layer (see module docstring):
       realpath containment always required; lexical-in-allowed-dir
       additionally accepted when ``allow_symlink_escapes`` is True
    3. Applies ``normcase()`` for Windows case-insensitive comparison
    4. Prefix-boundary guard: ``startswith(allowed_abs + os.sep)`` prevents
       ``/data-evil`` from matching ``/data``
    5. Exact match also accepted (path == allowed_abs)

    Args:
        path: The path to check (``~`` is expanded).
        allowed_paths: Allowlist of directories (``~`` expanded per entry).
        allow_symlink_escapes: When True, a path whose LEXICAL location is
            inside an allowed directory is permitted even if its symlink
            target is not (opt-in; default False = strict realpath-only
            containment).

    Returns:
        True if path is allowed, False if denied.
    """
    if not allowed_paths:
        # Empty list = nothing is allowed (fail-closed / default-deny)
        return False

    path = os.path.expanduser(path)
    abs_path = os.path.realpath(path)
    abs_path_norm = os.path.normcase(abs_path)

    # Layer A — realpath containment (the containment boundary; always on).
    for allowed in allowed_paths:
        allowed = os.path.expanduser(allowed)
        allowed_abs = os.path.realpath(allowed)
        allowed_norm = os.path.normcase(allowed_abs)
        if abs_path_norm == allowed_norm or abs_path_norm.startswith(allowed_norm + os.sep):
            return True

    # Layer B — lexical location inside an allowed dir (opt-in).
    # Permits distro/venv symlinks such as /etc/os-release → /usr/lib/os-release
    # when /etc is in allowed_paths. Default OFF (strict realpath-only
    # containment); opt in via [security] allow_symlink_escapes = true.
    if allow_symlink_escapes:
        expanded_original = os.path.expanduser(path)
        abs_original = os.path.abspath(expanded_original)
        original_norm = os.path.normcase(abs_original)
        for allowed in allowed_paths:
            allowed_abs = os.path.realpath(os.path.expanduser(allowed))
            allowed_norm = os.path.normcase(allowed_abs)
            if original_norm == allowed_norm or original_norm.startswith(allowed_norm + os.sep):
                return True

    return False
