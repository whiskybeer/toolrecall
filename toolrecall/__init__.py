"""
ToolRecall — Universal Tool Cache for LLM Agents.

Nutzung:
    from toolrecall import cached_read, cached_skill, cached_terminal, docs_search

    # Statt read_file():
    result = cached_read('/pfad/datei.md')

    # Statt skill_view():
    skill = cached_skill('skill-name')

    # Statt terminal('git status'):
    result = cached_terminal('git status')

    # Statt Web-Suche:
    info = docs_search('query')
"""

from toolrecall.cache import cached_read, cached_skill, cached_terminal, invalidate_all
from toolrecall.docs import docs_search, docs_get_page
from toolrecall.config import Config
from toolrecall.cli import main as cli_main

__version__ = "0.1.0"
__all__ = [
    "cached_read",
    "cached_skill",
    "cached_terminal",
    "docs_search",
    "docs_get_page",
    "invalidate_all",
    "Config",
    "cli_main",
]