"""ToolRecall — Configuration system.

Loads toolrecall.toml from:
1. Current working directory
2. ~/.config/toolrecall/toolrecall.toml
3. /etc/toolrecall/toolrecall.toml
4. Package default (config.toml next to this file)
"""
import os
import tomllib
from pathlib import Path

DEFAULT_PATHS = [
    Path("toolrecall.toml"),
    Path.home() / ".config" / "toolrecall" / "toolrecall.toml",
    Path("/etc/toolrecall/toolrecall.toml"),
]


class Config:
    """ToolRecall configuration — load once, access via attributes."""

    def __init__(self, path: str = None):
        self._data = self._load(path)
        self._expand_paths()

    def _load(self, path: str = None) -> dict:
        # Package default
        pkg_default = Path(__file__).parent / "config.toml"
        try:
            with open(pkg_default, "rb") as f:
                config = tomllib.load(f)
        except Exception:
            config = {}

        # User config (overrides defaults)
        if path:
            paths = [Path(path)]
        else:
            paths = DEFAULT_PATHS

        for p in paths:
            if p.exists():
                try:
                    with open(p, "rb") as f:
                        user = tomllib.load(f)
                    self._deep_merge(config, user)
                except Exception:
                    pass

        return config

    def _deep_merge(self, base: dict, override: dict):
        for key, val in override.items():
            if key in base and isinstance(base[key], dict) and isinstance(val, dict):
                self._deep_merge(base[key], val)
            else:
                base[key] = val

    def _expand_paths(self):
        """Expand ~ and environment variables in all string values."""
        def expand(val):
            if isinstance(val, str):
                return os.path.expandvars(os.path.expanduser(val))
            return val

        for section in self._data.values():
            if isinstance(section, dict):
                for key, val in section.items():
                    if isinstance(val, str):
                        section[key] = expand(val)
                    elif isinstance(val, dict):
                        for k, v in val.items():
                            if isinstance(v, str):
                                val[k] = expand(v)

    def get(self, *keys, default=None):
        val = self._data
        for k in keys:
            if isinstance(val, dict):
                val = val.get(k)
                if val is None:
                    return default
            else:
                return default
        return val if val is not None else default

    @property
    def cache_db(self) -> str:
        return self.get("paths", "cache_db", default="~/.toolrecall/cache.db")

    @property
    def knowledge_db(self) -> str:
        return self.get("paths", "knowledge_db", default="~/.toolrecall/knowledge.db")

    @property
    def file_ttl(self) -> int:
        return self.get("cache", "file_ttl", default=-1)

    @property
    def terminal_default_ttl(self) -> int:
        return self.get("cache", "terminal_default_ttl", default=300)

    def terminal_ttl(self, command: str) -> int:
        ttls = self.get("cache", "terminal_ttls", default={})
        if isinstance(ttls, dict):
            for cmd, ttl in ttls.items():
                if command.strip() == cmd.strip():
                    return ttl
        return self.terminal_default_ttl

    @property
    def proxy_port(self) -> int:
        return self.get("proxy", "port", default=8511)

    @property
    def proxy_bind(self) -> str:
        return self.get("proxy", "bind", default="127.0.0.1")

    @property
    def nginx_recommended(self) -> bool:
        return self.get("proxy", "nginx_recommended", default=True)

    @property
    def nginx_auto_site(self) -> bool:
        return self.get("proxy", "nginx_auto_site", default=False)


_config = None


def load_config(path: str = None) -> Config:
    """Load once, then reuse (singleton)."""
    global _config
    if _config is None or path:
        _config = Config(path)
    return _config