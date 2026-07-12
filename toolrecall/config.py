"""ToolRecall — Configuration system.

Loads toolrecall.toml from (in order of priority):
1. Environment variables (TOOLRECALL_*)
2. Current working directory (toolrecall.toml)
3. ~/.config/toolrecall/toolrecall.toml
4. /etc/toolrecall/toolrecall.toml
5. Package default (config.toml next to this file)
"""
import os
import sys
from pathlib import Path

# tomllib is Python 3.11+ stdlib; fall back to tomli (3.7+) for older Pythons
if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib  # pip install tomli (backport)

# MCP Server Registry for auto-resolution
from toolrecall.mcp_registry import resolve_server as _resolve_mcp_server

DEFAULT_PATHS = [
    Path("toolrecall.toml"),
    Path.home() / ".config" / "toolrecall" / "toolrecall.toml",
    Path("/etc/toolrecall/toolrecall.toml"),
]

ENV_MAP = {
    "TOOLRECALL_CACHE_DB": ("paths", "cache_db"),
    "TOOLRECALL_KNOWLEDGE_DB": ("paths", "knowledge_db"),
    "TOOLRECALL_SKILL_DIRS": ("paths", "skill_dirs"),
    "TOOLRECALL_FILE_TTL": ("cache", "file_ttl"),
    "TOOLRECALL_TERMINAL_TTL": ("cache", "terminal_default_ttl"),
    "TOOLRECALL_SCAN_DIRS": ("sources", "scan_dirs"),
    "TOOLRECALL_NGINX_DOMAIN": ("nginx", "domain"),
    "TOOLRECALL_MCP_ALLOWED_PATHS": ("mcp", "allowed_paths"),
    "TOOLRECALL_MCP_ALLOW_TERMINAL": ("mcp", "allow_terminal"),
    "TOOLRECALL_MCP_ALLOW_INVALIDATE": ("mcp", "allow_invalidate"),
    "TOOLRECALL_MCP_MULTIPLEX_ENABLED": ("mcp_multiplex", "enabled"),
    "TOOLRECALL_MCP_MULTIPLEX_SERVERS": ("mcp_multiplex", "servers"),
    "TOOLRECALL_MCP_MULTIPLEX_TRANSPARENT_CACHE": ("mcp_multiplex", "transparent_cache"),
    "TOOLRECALL_MCP_MULTIPLEX_DEFAULT_TTL": ("mcp_multiplex", "default_ttl"),
    "TOOLRECALL_STORAGE_BACKEND": ("storage", "backend"),
    "TOOLRECALL_HASH_ALGORITHM": ("cache", "hash_algorithm"),
    "TOOLRECALL_LOG_SHELL_FALLBACK": ("cache", "log_shell_fallback"),
    "TOOLRECALL_NORM_ENABLED": ("norm", "enabled"),
    "TOOLRECALL_NORM_SORT_LISTS": ("norm", "sort_lists"),
    "TOOLRECALL_NORM_STRIP_STRINGS": ("norm", "strip_strings"),
    "TOOLRECALL_SHIM_EXCLUDE_PREFIXES": ("shim", "exclude_prefixes"),
}


def _apply_env_overrides(config: dict) -> dict:
    # Track which keys expect booleans (need explicit string→bool coercion)
    _BOOL_KEYS = frozenset({
        "allow_terminal", "allow_invalidate", "enabled",
        "transparent_cache", "cognitive_check_enabled",
        "ast_check_enabled", "tool_access_control",
        "sort_lists", "strip_strings",
    })
    for env_key, (section, key) in ENV_MAP.items():
        val = os.environ.get(env_key)
        if val is None:
            continue
        if section not in config:
            config[section] = {}
        # Explicit bool coercion for boolean-typed keys
        if key in _BOOL_KEYS:
            lower = val.strip().lower()
            if lower in ("true", "1", "yes"):
                config[section][key] = True
            elif lower in ("false", "0", "no"):
                config[section][key] = False
            else:
                pass  # Leave default — invalid bool value
            continue
        try:
            config[section][key] = int(val)
            continue
        except (ValueError, TypeError):
            pass
        if key == "allowed_paths" and isinstance(val, str):
            # allowed_paths is always a list — split by comma or wrap as single item
            if "," in val:
                config[section][key] = [v.strip() for v in val.split(",") if v.strip()]
            else:
                config[section][key] = [val]
        elif "," in val:
            config[section][key] = [v.strip() for v in val.split(",") if v.strip()]
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
    def agent_home(self) -> str:
        """Resolve the agent home directory.

        Priority:
          1. AGENT_HOME env var
          2. TOOLRECALL_AGENT_HOME env var
          3. config [paths].agent_home setting
          4. ~/.hermes (backward compat, generic fallback)

        Agents should set AGENT_HOME so ToolRecall finds skills,
        configs, and memories correctly.
        """
        env = os.environ.get("AGENT_HOME") or os.environ.get("TOOLRECALL_AGENT_HOME")
        if env:
            return os.path.expanduser(env)
        cfg = self.get("paths", "agent_home", default=None)
        if cfg:
            return os.path.expanduser(cfg)
        return os.path.expanduser("~/.hermes")

    @property
    def skill_dirs(self) -> list:
        """Directories to search for skills.

        Priority:
          1. TOOLRECALL_SKILL_DIRS env var (comma-separated)
          2. config [paths].skill_dirs
          3. agent_home/skills (default)

        Returns list of expanded, absolute paths.
        """
        raw = self.get("paths", "skill_dirs", default=None)
        if raw:
            if isinstance(raw, list):
                return [os.path.expanduser(p) for p in raw]
            if isinstance(raw, str):
                return [os.path.expanduser(p) for p in raw.split(",")]
        # Default: agent_home/skills
        return [os.path.join(self.agent_home, "skills")]

    # ─── Storage Configuration ────────────────────────

    @property
    def storage_backend(self) -> str:
        """Storage backend for caching. Default: 'sqlite'. Future: 'redis', 'postgres'."""
        val = self.get("storage", "backend", default="sqlite")
        return str(val).lower() if val else "sqlite"

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

    # ─── MCP Security Properties ──────────────────────

    @property
    def mcp_allowed_paths(self) -> list:
        """Paths allowed for cached_read.

        Default-deny security: when this list is empty, NO paths are readable
        through ToolRecall's MCP file-caching tools. The user MUST explicitly
        add directories here — there is no fallback to "allow everything".

        Consequences of allowing a path:
          - Any file under that path becomes readable through ToolRecall's MCP
            file-caching tools (cached_read, cached_mcp tools)
          - If the agent is prompt-injected, an attacker can read files under
            allowed paths via the MCP layer
          - The sensitive file blocklist (.env, .ssh/, .pem, etc.) still
            applies within allowed paths as a secondary safety net
          - Files outside allowed paths are always rejected — even with
            a direct cache lookup

        Example safe defaults:
            allowed_paths = ["~/projects", "~/.hermes/skills"]
        """
        raw = self.get("mcp", "allowed_paths", default=[])
        if raw is None:
            return []
        return [os.path.expanduser(p) for p in raw]

    @property
    def mcp_allow_terminal(self) -> bool:
        """Allow cached_terminal tool (default: False — security risk)."""
        return self.get("mcp", "allow_terminal", default=False)
        
    @property
    def mcp_allowed_terminal_commands(self) -> list:
        """Regex allowlist for terminal commands (e.g. ['^npm run (lint|test)$']).
        If list is empty but allow_terminal=True, all commands are allowed (DANGEROUS).
        """
        raw = self.get("mcp", "allowed_terminal_commands", default=[])
        return raw if isinstance(raw, list) else []

    @property
    def mcp_allowed_terminal_patterns(self) -> list:
        """Compiled regex patterns from allowed_terminal_commands.
        Empty list = no terminal commands allowed via MCP.
        """
        import re
        raw = self.mcp_allowed_terminal_commands
        if not raw:
            return []
        patterns = []
        for p in raw:
            try:
                patterns.append(re.compile(p))
            except re.error:
                pass
        return patterns

    @property
    def mcp_allow_invalidate(self) -> bool:
        """Allow cache_invalidate tool (default: False)."""
        return self.get("mcp", "allow_invalidate", default=False)

    @property
    def mcp_cognitive_check_enabled(self) -> bool:
        """Enable cognitive semantic scan on tool arguments (default: True)."""
        return bool(self.get("security", "cognitive_check_enabled", default=True))

    @property
    def mcp_ast_check_enabled(self) -> bool:
        """Enable AST structural validation on tool arguments (default: True)."""
        return bool(self.get("security", "ast_check_enabled", default=True))

    @property
    def mcp_tool_access_control(self) -> bool:
        """Keyword-based access control for MCP tools.

        Blocks tools whose names contain dangerous substrings (write, delete, etc.).
        This is a STRING MATCH on tool names — not process isolation, not an OS sandbox.
        A tool named 'post_message' passes through even if it modifies state.
        For real isolation, pair with Docker/gVisor.
        """
        return bool(self.get("security", "tool_access_control", default=False))

    @property
    def mcp_dangerous_tool_keywords(self) -> list:
        """Keywords that indicate a tool modifies state. Used by tool_access_control."""
        default_keywords = ["write", "edit", "delete", "remove", "terminal", "bash", "exec", "run", "push", "commit", "update", "create"]
        val = self.get("security", "dangerous_tool_keywords", default=default_keywords)
        return val if isinstance(val, list) else default_keywords

    # ─── MCP Multiplex Properties ─────────────────────

    @property
    def mcp_multiplex_enabled(self) -> bool:
        """Enable MCP Multiplexer (default: True)."""
        return self.get("mcp_multiplex", "enabled", default=True)

    @property
    def mcp_multiplex_servers(self) -> list:
        """Allowlist of server names to multiplex. Empty = all configured."""
        return self.get("mcp_multiplex", "servers", default=[])

    @property
    def mcp_multiplex_idle_minutes(self) -> int:
        """Minutes of inactivity before an MCP server is shut down (default: 15).
        Set to 0 to keep servers alive forever."""
        return self.get("mcp_multiplex", "idle_minutes", default=15)

    @property
    def mcp_multiplex_transparent_cache(self) -> bool:
        """Transparent caching: cache MCP tool responses automatically.
        When true, every tools/call response is cached by default (TTL per server).
        Works with ANY MCP client — not Hermes-specific."""
        return self.get("mcp_multiplex", "transparent_cache", default=True)

    @property
    def mcp_multiplex_default_ttl(self) -> int:
        """Default TTL for transparently cached MCP responses (seconds).
        Per-server TTLs in servers_config override this."""
        return self.get("mcp_multiplex", "default_ttl", default=60)

    @property
    def mcp_multiplex_servers_config(self) -> dict:
        """Resolve server configurations from the registry + optional overrides.

        Resolution priority (highest wins):
          1. Explicit [mcp_multiplex.servers_config] in config.toml
          2. Auto-resolved from registry for names in the `servers` list
             that aren't already explicitly configured

        Returns dict: {name: {command, args, env, ttl, ...}}
        """
        # 1. Explicit config.toml servers_config (user overrides)
        raw = self.get("mcp_multiplex", "servers_config", default={})
        result = dict(raw) if isinstance(raw, dict) else {}

        # 2. Auto-resolve remaining server names from registry
        allow_servers = self.mcp_multiplex_servers
        for name in allow_servers:
            name_lower = name.lower()
            if name_lower in result:
                continue  # Already explicitly configured — no override
            resolved = _resolve_mcp_server(name_lower)
            if resolved is not None:
                cmd, args, source = resolved
                # Skip external servers that require uvx when uvx is not installed
                from toolrecall.mcp_registry import has_uvx
                if source == "external" and cmd == "uvx" and not has_uvx():
                    continue
                result[name_lower] = {
                    "name": name,
                    "command": cmd,
                    "args": list(args),
                }
                if source == "builtin":
                    # Use the active Python interpreter for built-in servers
                    result[name_lower]["command"] = sys.executable

        return result

    # ─── Norm Properties ───────────────────────────────

    @property
    def norm_sort_lists(self) -> bool:
        """Sort lists of primitive types during normalization (default: True).
        Set False to preserve argument order (e.g. for positional args).
        Configured via [norm].sort_lists in toolrecall.toml.
        """
        return bool(self.get("norm", "sort_lists", default=True))

    @property
    def norm_strip_strings(self) -> bool:
        """Strip leading/trailing whitespace during normalization (default: True).
        Set False to preserve significant whitespace in string values.
        Configured via [norm].strip_strings in toolrecall.toml.
        """
        return bool(self.get("norm", "strip_strings", default=True))

    # ─── Shim Properties ────────────────────────────────

    @property
    def shim_exclude_prefixes(self) -> list:
        """Path prefixes to skip when the shim intercepts open() calls.

        Files matching these prefixes are read directly (bypassing the cache).
        Useful for internal infrastructure files that are rewritten constantly
        and never benefit from caching (e.g. Hermes /tmp/hermes-cwd-* files).

        Configured via [shim].exclude_prefixes in toolrecall.toml,
        or TOOLRECALL_SHIM_EXCLUDE_PREFIXES env var (comma-separated).

        Empty list = bypass NOTHING (all open() calls go through the shim).
        """
        raw = self.get("shim", "exclude_prefixes", default=[])
        if isinstance(raw, list):
            return raw
        return []

# ─── save_config / load_config ────────────────────────

_CONFIG = None


def save_config(path: str, config: Config) -> bool:
    """Write a Config instance back to a TOML file.

    Uses the built-in TOML serializer — no external dependencies needed.

    Args:
        path: Output path (e.g. '~/.config/toolrecall/toolrecall.toml')
        config: Config instance to serialize.

    Returns True on success, False on failure.
    """
    from toolrecall.toml_serializer import dump
    path = os.path.expanduser(path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        with open(path, "w") as f:
            dump(config._data, f, comment="ToolRecall Configuration")
        return True
    except Exception:
        return False


def load_config(path: str = None) -> Config:
    """Load ToolRecall configuration. Returns a fresh Config instance each call.

    Unlike the previous singleton pattern, this always creates a new Config
    so that TOOLRECALL_* environment variables set after the first import
    are respected. Callers that want caching should store the result.
    """
    return Config(path)
