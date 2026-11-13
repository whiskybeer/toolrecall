"""Where the agent and the product keep their files — stdlib only.

Both answers are needed on the shim's config-failure path, where importing
``toolrecall.config`` is exactly what failed, so this module must not import
anything from the package.

Two cascades live here for one reason: they already drifted as duplicated
copies (``Config.agent_home`` vs the shim's except branch, found 2026-11-11).
Each resolves as ``env override → documented default``; ``Config`` owns the
middle step (the ``[paths]`` setting), because that needs the config file.

Agents should set ``AGENT_HOME``. ``~/.hermes`` remains the compat default.
"""

from __future__ import annotations

import os

DEFAULT_AGENT_HOME = "~/.hermes"
DEFAULT_DATA_DIR = "~/.toolrecall"


def agent_home_from_env() -> str:
    """Configured agent home from the environment, or '' when unset."""
    raw = os.environ.get("AGENT_HOME") or os.environ.get("TOOLRECALL_AGENT_HOME")
    return os.path.expanduser(raw) if raw else ""


def agent_home_default() -> str:
    return os.path.expanduser(DEFAULT_AGENT_HOME)


def agent_home() -> str:
    """Environment first, compat default otherwise (no config file involved)."""
    return agent_home_from_env() or agent_home_default()


def data_dir_from_env() -> str:
    """Configured ToolRecall data dir from the environment, or '' when unset."""
    raw = os.environ.get("TOOLRECALL_DATA_DIR")
    return os.path.expanduser(raw) if raw else ""


def data_dir_default() -> str:
    return os.path.expanduser(DEFAULT_DATA_DIR)


def data_dir() -> str:
    """Environment first, product default otherwise (no config file involved)."""
    return data_dir_from_env() or data_dir_default()
