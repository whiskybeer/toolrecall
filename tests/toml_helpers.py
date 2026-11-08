"""TOML helpers for config files written by test fixtures.

Windows paths (``C:\\Users\\...``) break raw TOML interpolation: ``\\U`` is
an invalid escape in a TOML basic string, tomllib raises, and
``Config._load`` degrades the whole file to package defaults with a single
warning. Any test whose fixture config failed to load was silently testing
defaults — the a6 namespacing tests passed on Windows for exactly this
vacuous reason (every member fell back to the same namespace).

Rule: never f-string a filesystem path into TOML directly. Route every
path through :func:`toml_str` / :func:`write_toml_config` so a broken
fixture config fails loudly at setup, not vacuously at assert time.
"""

import json
import os


def toml_str(value) -> str:
    """TOML-safe basic-string literal for any filesystem path (or str).

    ``json.dumps`` double-quotes and escapes backslashes; its escape set
    is a subset of TOML basic-string escapes (TOML v1.0.0 adopts the
    JSON ``\\uXXXX`` / ``\\\\`` forms), so the result is a valid TOML
    basic string on every platform.
    """
    return json.dumps(str(value))


def write_toml_config(path, body: str) -> str:
    """Write *body* to *path*, then assert it parses, then return the path.

    The parse assertion is the point: a config a test just wrote must
    never fail to load — ``Config._load`` would warn-and-fallback and the
    test would exercise package defaults instead of the fixture values.
    Failing here (loudly, at setup) beats an assertion that passes for
    the wrong reason.
    """
    path = os.fspath(path)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(body)

    import tomllib

    with open(path, "rb") as f:
        tomllib.load(f)  # raises on invalid TOML — loud, at test setup
    return path
