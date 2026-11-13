"""Deprecated alias — the resolvers now live in ``toolrecall.home_dirs``.

Kept as a thin re-export so an external import of ``toolrecall.agent_home``
(added in 0.8.24) keeps working. New code should import ``home_dirs``; this
module will be removed in a future minor release.
"""

from __future__ import annotations

from toolrecall.home_dirs import agent_home as resolve  # noqa: F401
from toolrecall.home_dirs import agent_home_default as default  # noqa: F401
from toolrecall.home_dirs import agent_home_from_env as from_env  # noqa: F401
