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
    "TOOLRECALL_MCP_ALLOWED_PATHS": ("mcp", "allowed_paths"),
    "TOOLRECALL_MCP_ALLOW_TERMINAL": ("mcp", "allow_terminal"),
    "TOOLRECALL_MCP_ALLOW_INVALIDATE": ("mcp", "allow_invalidate"),
    "TOOLRECALL_MCP_MULTIPLEX_ENABLED": ("mcp_multiplex", "enabled"),
    "TOOLRECALL_MCP_MULTIPLEX_SERVERS": ("mcp_multiplex", "servers"),
    "TOOLRECALL_MCP_MULTIPLEX_HERMES_CONFIG": ("mcp_multiplex", "hermes_config"),
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

    # ─── MCP Security Properties ──────────────────────

    @property
    def mcp_allowed_paths(self) -> list:
        """Paths allowed for cached_read. Empty list = all paths (DANGEROUS)."""
        raw = self.get("mcp", "allowed_paths", default=[])
        if raw is None:
            return []
        return [os.path.expanduser(p) for p in raw]

    @property
    def mcp_allow_terminal(self) -> bool:
        """Allow cached_terminal tool (default: False — security risk)."""
        return self.get("mcp", "allow_terminal", default=False)

    @property
    def mcp_allow_invalidate(self) -> bool:
        """Allow cache_invalidate tool (default: False)."""
        return self.get("mcp", "allow_invalidate", default=False)

    # ─── MCP Multiplex Properties ─────────────────────

    @property
    def mcp_multiplex_enabled(self) -> bool:
        """Enable MCP Multiplexer (default: True)."""
        return self.get("mcp_multiplex", "enabled", default=True)

    @property
    def mcp_multiplex_servers(self) -> list:
        """Whitelist of server names to multiplex. Empty = all configured."""
        return self.get("mcp_multiplex", "servers", default=[])

    @property
    def mcp_multiplex_hermes_config(self) -> str:
        """Path to Hermes config.yaml for discovering MCP servers.
        Empty = auto-detect (looks at ~/.hermes/config.yaml)."""
        return self.get("mcp_multiplex", "hermes_config", default="")

    @property
    def mcp_multiplex_idle_minutes(self) -> int:
        """Minutes of inactivity before an MCP server is shut down (default: 15).
        Set to 0 to keep servers alive forever."""
        return self.get("mcp_multiplex", "idle_minutes", default=15)

    @property
    def mcp_multiplex_servers_config(self) -> dict:
        """Parse Hermes-style mcp_servers config from toolrecall config or Hermes config.yaml.

        The [mcp_multiplex.servers_config] section follows the same format as
        Hermes' mcp_servers, but if empty, we auto-detect from Hermes config.
        """
        # First try toolrecall's own config section
        raw = self.get("mcp_multiplex", "servers_config", default=None)
        if raw and isinstance(raw, dict):
            return raw

        # Fall back to Hermes config
        hermes_cfg_path = self.mcp_multiplex_hermes_config
        if not hermes_cfg_path:
            hermes_cfg_path = os.path.expanduser("~/.hermes/config.yaml")

        if os.path.exists(hermes_cfg_path):
            try:
                # Simple YAML-free parser for mcp_servers section
                return self._parse_hermes_mcp_servers(hermes_cfg_path)
            except Exception:
                pass

        return {}

    def _parse_hermes_mcp_servers(self, path: str) -> dict:
        """Minimal YAML parser to extract mcp_servers from Hermes config.yaml.
        
        Handles the known structure:
          mcp_servers:
            github:
              command: npx
              args: ["-y", "@modelcontextprotocol/server-github"]
              env: {KEY: value}
        """
        servers = {}
        with open(path, "r") as f:
            lines = f.readlines()

        in_mcp_servers = False
        current_server = None
        current_config = {}
        indent_level = 0

        for line in lines:
            stripped = line.rstrip()
            if not stripped or stripped.strip().startswith("#"):
                continue

            # Detect mcp_servers block
            if stripped.strip() == "mcp_servers:" and not stripped.startswith(" "):
                in_mcp_servers = True
                indent_level = 0
                continue

            if not in_mcp_servers:
                continue

            # Check if we've left the mcp_servers block
            cur_indent = len(line) - len(line.lstrip())
            if cur_indent == 0 and line.strip() and ":" in line:
                in_mcp_servers = False
                break

            # Server name (e.g., "  github:")
            if cur_indent == 2 and stripped.strip().endswith(":"):
                if current_server and current_config:
                    servers[current_server] = current_config
                current_server = stripped.strip().rstrip(":")
                current_config = {"command": "", "args": [], "env": {}}
                indent_level = 2
                continue

            # Properties like command, args
            if current_server:
                key_val = stripped.strip()
                if ": " in key_val:
                    key, val = key_val.split(": ", 1)
                    key = key.strip()
                    if key == "command":
                        current_config["command"] = val.strip().strip('"').strip("'")
                        # Handle multi-word commands like "uv run"
                        cmd_parts = current_config["command"].split()
                        if len(cmd_parts) > 1:
                            current_config["command"] = cmd_parts[0]
                            extra_args = cmd_parts[1:]
                            current_config.setdefault("args", [])
                            current_config["args"] = extra_args + current_config["args"]
                    elif key == "args":
                        # Parse inline list like "[-y, @modelcontextprotocol/...]"
                        # or it might be on next lines with "- item"
                        val = val.strip()
                        if val.startswith("["):
                            args = []
                            for item in val.strip("[]").split(","):
                                item = item.strip().strip('"').strip("'")
                                if item:
                                    args.append(item)
                            current_config["args"] = args
                        else:
                            current_config["args"] = []
                    elif key == "timeout":
                        try:
                            current_config["timeout"] = int(val)
                        except ValueError:
                            pass
                elif stripped.strip().startswith("- "):
                    # Array items on separate lines
                    item = stripped.strip()[2:].strip().strip('"').strip("'")
                    if key_val.count('"') <= 2:  # simple string item
                        current_config.setdefault("args", []).append(item)

        # Don't forget the last server
        if current_server and current_config:
            servers[current_server] = current_config

        # Filter out toolrecall itself to avoid circular startup
        servers.pop("toolrecall", None)
        return servers


_config = None


def load_config(path: str = None) -> Config:
    global _config
    if _config is None or path:
        _config = Config(path)
    return _config
