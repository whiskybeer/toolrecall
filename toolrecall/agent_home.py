"""Agent-home resolution — one cascade, importable with nothing but the stdlib.

Two consumers need it:

* ``Config.agent_home`` — the normal path (env → ``[paths].agent_home`` →
  ``~/.hermes``);
* the shim's ``_load_skip_prefixes`` except branch — the path where config
  loading has *failed*, so it cannot call back into the config module.

They were two copies of the cascade, which is how they drift. This module is
deliberately dependency-free so the broken-config path can still import it.

``~/.hermes`` remains the generic fallback for backward compatibility; agents
should set ``AGENT_HOME`` instead of relying on it.
"""

from __future__ import annotations

import os

DEFAULT_HOME = "~/.hermes"


def from_env() -> str:
    """The configured agent home from the environment, or '' when unset."""
    raw = os.environ.get("AGENT_HOME") or os.environ.get("TOOLRECALL_AGENT_HOME")
    return os.path.expanduser(raw) if raw else ""


def default() -> str:
    """The generic fallback home (expanded)."""
    return os.path.expanduser(DEFAULT_HOME)


def resolve() -> str:
    """Environment first, generic fallback otherwise (no config file involved)."""
    return from_env() or default()
