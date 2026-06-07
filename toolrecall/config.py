"""ToolRecall — Configuration system.

Loads toolrecall.toml from (in order of priority):
1. Environment variables (TOOLRECALL_*)
2. Current working directory (toolrecall.toml)
3. ~/.config/toolrecall/toolrecall.toml
4. /etc/toolrecall/toolrecall.toml
5. Package default (config.toml next to this file)
"""
import os
import tomllib
from pathlib import Path

DEFAULT_PATHS = [
    Path("toolrecall.toml"),
    Path.home() / ".config" / "toolrecall" / "toolrecall.toml",
    Path("/etc/toolrecall/toolrecall.toml"),
]

ENV_MAP = {
    "TOOLRECALL_CACHE_DB": ("paths", "cache_db"),
    "TOOLRECALL_KNOWLEDGE_DB": ("paths", "knowledge_db"),
    "TOOLRECALL_FILE_TTL": ("cache", "file_ttl"),
    "TOOLRECALL_TERMINAL_TTL": ("cache", "terminal_default_ttl"),
    "TOOLRECALL_PROXY_PORT": ("proxy", "port"),
    "TOOLRECALL_PROXY_BIND": ("proxy", "bind"),
    "TOOLRECALL_SCAN_DIRS": ("sources", "scan_dirs"),
    "TOOLRECALL_NGINX_DOMAIN": ("nginx", "domain"),
}


def _apply_env_overrides(config: dict) -> dict:
    for env_key, (section, key) in ENV_MAP.items():
        val = os.environ.get(env_key)
        if val is None:
            continue
        if section not in config:
            config[section] = {}
        try:
            config[section][key] = int(val)
            continue
        except (ValueError, TypeError):
            pass
        if "," in val:
            config[section][key] = [v.strip() for v in val.split(",")]
        else:
            config[section][key] = val
    return config


class Config:
    """ToolRecall configuration — load once, access via attributes.

    Priority (higher wins):
        env vars > CWD config > ~/.config > /etc > package default
    """

    def __init__(self, path: str = None):
        self._data = self._load(path)
        self._expand_paths()

    def _load(self, path: str = None) -> dict:
        pkg_default = Path(__file__).parent / "config.toml"
        try:
            with open(pkg_default, "rb") as f:
                config = tomllib.load(f)
        except Exception:
            config = {}

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

        config = _apply_env_overrides(config)
        return config

    def _deep_merge(self, base: dict, override: dict):
        for key, val in override.items():
            if key in base and isinstance(base[key], dict) and isinstance(val, dict):
                self._deep_merge(base[key], val)
            else:
                base[key] = val

    def _expand_paths(self):
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
        return self.get("proxy", "port", default=8567)

    @property
    def proxy_bind(self) -> str:
        return self.get("proxy", "bind", default="[IP_ADDRESS]")

    @property
    def nginx_recommended(self) -> bool:
        return self.get("proxy", "nginx_recommended", default=True)

    @property
    def nginx_auto_site(self) -> bool:
        return self.get("proxy", "nginx_auto_site", default=False)


_config = None


def load_config(path: str = None) -> Config:
    global _config
    if _config is None or path:
        _config = Config(path)
    return _config
