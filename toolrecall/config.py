"""ToolRecall — Configuration system.

Loads toolrecall.toml from (in order of priority):
1. Environment variables (TOOLRECALL_*)
2. Current working directory (toolrecall.toml)
3. ~/.config/toolrecall/toolrecall.toml
4. /etc/toolrecall/toolrecall.toml
5. Package default (config.toml next to this file)
"""

import logging
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

# Default cap on a single cached API response body. Kept in sync with
# cache.API_CACHE_MAX_BODY_MB (cache.py can't import from here at module load
# without a cycle, so both carry the same default and a cross-reference).
API_CACHE_MAX_BODY_MB_DEFAULT = 2.0

ENV_MAP = {
    "TOOLRECALL_CACHE_DB": ("paths", "cache_db"),
    "TOOLRECALL_KNOWLEDGE_DB": ("paths", "knowledge_db"),
    "TOOLRECALL_SKILL_DIRS": ("paths", "skill_dirs"),
    "TOOLRECALL_FILE_TTL": ("cache", "file_ttl"),
    "TOOLRECALL_MAX_FILE_MB": ("cache", "max_file_mb"),
    "TOOLRECALL_TERMINAL_TTL": ("cache", "terminal_default_ttl"),
    "TOOLRECALL_SCAN_DIRS": ("sources", "scan_dirs"),
    "TOOLRECALL_NGINX_DOMAIN": ("nginx", "domain"),
    "TOOLRECALL_MCP_ALLOWED_PATHS": ("mcp", "allowed_paths"),
    "TOOLRECALL_MCP_ALLOW_TERMINAL": ("mcp", "allow_terminal"),
    "TOOLRECALL_MCP_ALLOW_INVALIDATE": ("mcp", "allow_invalidate"),
    "TOOLRECALL_MCP_REQUIRE_VALIDATOR": ("mcp", "require_validator"),
    "TOOLRECALL_MCP_EMIT_CONTEXT_HINTS": ("mcp", "emit_context_hints"),
    "TOOLRECALL_MCP_MULTIPLEX_ENABLED": ("mcp_multiplex", "enabled"),
    "TOOLRECALL_MCP_MULTIPLEX_GATEWAY": ("mcp_multiplex", "gateway"),
    "TOOLRECALL_MCP_MULTIPLEX_GATEWAY_EXCLUDE": ("mcp_multiplex", "gateway_exclude"),
    "TOOLRECALL_MCP_MULTIPLEX_GATEWAY_EXCLUDE_TOOLS": (
        "mcp_multiplex",
        "gateway_exclude_tools",
    ),
    "TOOLRECALL_MCP_MULTIPLEX_SERVERS": ("mcp_multiplex", "servers"),
    "TOOLRECALL_MCP_MULTIPLEX_TRANSPARENT_CACHE": ("mcp_multiplex", "transparent_cache"),
    "TOOLRECALL_MCP_MULTIPLEX_FETCH_REVALIDATE": ("mcp_multiplex", "fetch_revalidate"),
    "TOOLRECALL_MCP_MULTIPLEX_DEFAULT_TTL": ("mcp_multiplex", "default_ttl"),
    "TOOLRECALL_STORAGE_BACKEND": ("storage", "backend"),
    "TOOLRECALL_LIBSQL_DB_PATH": ("storage", "libsql_db"),
    "TOOLRECALL_SYNC_ENABLED": ("storage", "sync_enabled"),
    "TOOLRECALL_SYNC_URL": ("storage", "sync_url"),
    "TOOLRECALL_SYNC_TOKEN": ("storage", "sync_token"),
    "TOOLRECALL_SYNC_INTERVAL": ("storage", "sync_interval"),
    "TOOLRECALL_TURSO_API_BASE": ("storage", "turso_api_base"),
    "TOOLRECALL_HASH_ALGORITHM": ("cache", "hash_algorithm"),
    "TOOLRECALL_LOG_SHELL_FALLBACK": ("cache", "log_shell_fallback"),
    "TOOLRECALL_NORM_ENABLED": ("norm", "enabled"),
    "TOOLRECALL_UPDATE_CHECK": ("update", "enabled"),
    "TOOLRECALL_NORM_SORT_LISTS": ("norm", "sort_lists"),
    "TOOLRECALL_NORM_STRIP_STRINGS": ("norm", "strip_strings"),
    "TOOLRECALL_SHIM_EXCLUDE_PREFIXES": ("shim", "exclude_prefixes"),
    "TOOLRECALL_SHIM_TAG_SOURCE": ("shim", "tag_source"),
    "TOOLRECALL_SHIM_STUB_TTL": ("shim", "stub_ttl"),
    "TOOLRECALL_SHIM_STUB_MAX_CONSECUTIVE": ("shim", "stub_max_consecutive"),
    "TOOLRECALL_TRACKER_IGNORE_PREFIXES": ("tracker", "ignore_read_prefixes"),
    "TOOLRECALL_TRACKER_PERSIST_TTL": ("tracker", "persist_ttl"),
    "TOOLRECALL_RECALL_ENABLED": ("recall", "enabled"),
    "TOOLRECALL_RECALL_TTL": ("recall", "ttl"),
    "TOOLRECALL_DOCS_INDEX_TTL": ("docs", "index_ttl"),
    # Knowledge-index compaction (see toolrecall.docs _maybe_compact_index)
    "TOOLRECALL_DOCS_COMPACT": ("docs", "compact"),
    "TOOLRECALL_DOCS_COMPACT_RATIO": ("docs", "compact_ratio"),
    "TOOLRECALL_DOCS_COMPACT_MIN_FTS_MB": ("docs", "compact_min_fts_mb"),
    "TOOLRECALL_DOCS_COMPACT_VACUUM": ("docs", "compact_vacuum"),
    # TTL policy: stale-while-revalidate + adaptive TTL (see toolrecall.ttl_policy)
    "TOOLRECALL_STALE_WHILE_REVALIDATE": ("cache", "stale_while_revalidate"),
    "TOOLRECALL_ADAPTIVE_TTL": ("cache", "adaptive_ttl"),
    "TOOLRECALL_ADAPTIVE_FACTOR": ("cache", "adaptive_factor"),
    "TOOLRECALL_ADAPTIVE_MAX_TTL": ("cache", "adaptive_max_ttl"),
    # Fuzzy TTL classification + canonical command keys (see normalizer.py)
    "TOOLRECALL_FUZZY_TTL_MATCH": ("cache", "fuzzy_ttl_match"),
    "TOOLRECALL_FUZZY_THRESHOLD": ("cache", "fuzzy_threshold"),
    "TOOLRECALL_CANONICAL_COMMANDS": ("norm", "canonical_commands"),
    # Resilience: coalescing / retry / circuit breaker (see resilience.py)
    "TOOLRECALL_RESILIENCE_COALESCING": ("resilience", "coalescing"),
    "TOOLRECALL_COALESCING_WINDOW": ("resilience", "coalescing_window"),
    "TOOLRECALL_RETRY": ("resilience", "retry"),
    "TOOLRECALL_RETRY_MAX_ATTEMPTS": ("resilience", "retry_max_attempts"),
    "TOOLRECALL_RETRY_BACKOFF_BASE": ("resilience", "retry_backoff_base"),
    "TOOLRECALL_CIRCUIT_BREAKER": ("resilience", "circuit_breaker"),
    "TOOLRECALL_CB_FAILURE_THRESHOLD": ("resilience", "cb_failure_threshold"),
    "TOOLRECALL_CB_WINDOW": ("resilience", "cb_window"),
    "TOOLRECALL_CB_OPEN_SECONDS": ("resilience", "cb_open_seconds"),
    # Shared network daemon (A6): opt-in TCP listener with HMAC auth.
    # All defaults OFF/loopback — see [network] in config.toml.
    "TOOLRECALL_NETWORK_ENABLED": ("network", "enabled"),
    "TOOLRECALL_NETWORK_BIND": ("network", "bind"),
    "TOOLRECALL_NETWORK_PORT": ("network", "port"),
    "TOOLRECALL_NETWORK_TOKEN": ("network", "token"),
    "TOOLRECALL_NETWORK_TLS": ("network", "tls"),
    "TOOLRECALL_NETWORK_ALLOW_PUBLIC": ("network", "allow_public"),
    "TOOLRECALL_NETWORK_TLS_CERT": ("network", "tls_cert"),
    "TOOLRECALL_NETWORK_TLS_KEY": ("network", "tls_key"),
    # Forward-proxy request-body dedup (opt-in — proxy.py _maybe_dedup_body).
    # Default OFF: the proxy never rewrites request bodies silently.
    "TOOLRECALL_FORWARD_DEDUP": ("forward_proxy", "dedup"),
    # Forward-proxy default canonicalization profile (proxy.py
    # _canonicalize_default). '' = header opt-in only; e.g. "hermes" applies
    # the profile to headerless requests (session-wide default).
    "TOOLRECALL_CANONICALIZE_DEFAULT": ("forward_proxy", "canonicalize_default"),
    # Forward-proxy bypass cache for sampled requests (explicit temperature > 0).
    # Default OFF: cache all byte-identical requests transparently.
    "TOOLRECALL_FORWARD_BYPASS_SAMPLED": ("forward_proxy", "bypass_sampled"),
    # SecurityGate symlink policy (path_utils Layer B, F1): lexical
    # in-allowed-dir symlinks permitted (default false — strict
    # realpath-only containment; true opts INTO Layer-B rescue).
    "TOOLRECALL_ALLOW_SYMLINK_ESCAPES": ("security", "allow_symlink_escapes"),
}


def _apply_env_overrides(config: dict) -> dict:
    # Track which keys expect booleans (need explicit string→bool coercion)
    _BOOL_KEYS = frozenset(
        {
            "allow_terminal",
            "allow_invalidate",
            "enabled",
            "transparent_cache",
            "cognitive_check_enabled",
            "ast_check_enabled",
            "tool_access_control",
            "sort_lists",
            "strip_strings",
            "sync_enabled",
            "adaptive_ttl",
            "fuzzy_ttl_match",
            "canonical_commands",
            "coalescing",
            "circuit_breaker",
            "dedup",
            "enabled",
            "allow_symlink_escapes",
            "compact",
            "compact_vacuum",
        }
    )
    # Float-typed keys (fail-soft override): an unparseable env value must
    # NOT replace a valid config-file value with a raw string (which the
    # property would then reject down to its default). 2026-09-18: invalid
    # TOOLRECALL_SHIM_STUB_TTL masked a valid [shim].stub_ttl=123 with the
    # 300 default — invalid env now falls back to CONFIG per the documented
    # contract, and only to the default when the config lacks the key too.
    _FLOAT_KEYS = frozenset({"stub_ttl", "persist_ttl", "compact_ratio", "compact_min_fts_mb"})
    # List-typed keys: an env value without a comma is still a one-element
    # list, not a bare string. Without this branch a single-value override
    # was stored as str, and the property's isinstance(raw, list) silently
    # dropped it back to defaults (TOOLRECALL_TRACKER_IGNORE_PREFIXES bug:
    # "/nonexistent/" never reached the tracker).
    _LIST_KEYS = frozenset(
        {
            "skill_dirs",
            "scan_dirs",
            "allowed_paths",
            "servers",
            "exclude_prefixes",
            "ignore_read_prefixes",
        }
    )
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
        if key in _FLOAT_KEYS:
            try:
                config[section][key] = float(val)
            except (ValueError, TypeError):
                logging.getLogger("toolrecall.config").debug(
                    "%s=%r is not a number — keeping config value", env_key, val
                )
            continue
        if key in _LIST_KEYS:
            if "," in val:
                config[section][key] = [v.strip() for v in val.split(",") if v.strip()]
            else:
                config[section][key] = [val]
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

    def __init__(self, path: str | None = None):
        self._data = self._load(path)
        self._expand_paths()
        # Wire [norm] into the normalizer's module defaults (B1): the keys
        # were plumbed (ENV_MAP, schema, norm_* properties) but no consumer
        # ever read the properties, so env/TOML overrides never changed how
        # cache keys were normalized. Config is loaded once per process, so
        # a single call here covers daemon, CLI, adapters and replay. Lazy
        # import: config.py must not import its consumers at module level.
        from toolrecall.normalizer import apply_config

        apply_config(self)

    def _load(self, path: str | None = None) -> dict:
        pkg_default = Path(__file__).parent / "config.toml"
        try:
            with open(pkg_default, "rb") as f:
                config = tomllib.load(f)
        except Exception:
            config = {}

        if path:
            paths = [Path(path)]
        else:
            # TOOLRECALL_CONFIG: explicit config-file override (same var the
            # daemon/cli docs reference). Appended LAST so it wins over the
            # default discovery paths (cwd > ~/.config > /etc > env-cfg);
            # an explicit `path` arg still wins over everything.
            paths = list(DEFAULT_PATHS)
            env_cfg = os.environ.get("TOOLRECALL_CONFIG")
            if env_cfg:
                paths.append(Path(env_cfg))

        for p in paths:
            if p.exists():
                try:
                    with open(p, "rb") as f:
                        user = tomllib.load(f)
                    self._warn_unknown_keys(config, user, str(p))
                    self._deep_merge(config, user)
                except Exception as e:
                    # Fail-open load failure (review 2026-09): a malformed
                    # config degrades to PACKAGE DEFAULTS — for a file that
                    # also carries [mcp] allowed_paths and [network] tokens,
                    # that flips security posture silently. Make the
                    # degradation impossible to miss.
                    import warnings

                    warnings.warn(
                        f"ToolRecall: CONFIG IGNORED — could not load {p}: "
                        f"{type(e).__name__}: {e}. Running on package "
                        f"defaults; [mcp] allowed_paths, [network] tokens "
                        f"and every other setting from this file are NOT "
                        f"active. Fix or remove the file.",
                        stacklevel=2,
                    )

        config = _apply_env_overrides(config)
        return config

    # Valid sections and their keys — union of the package default
    # config.toml and every config.get()/self.get() key the code reads.
    # Anything else in a user config is almost always a typo (e.g.
    # "allowed_path" vs "allowed_paths") that used to be silently ignored —
    # the daemon then ran with the default-deny default and the user had no
    # idea why. Dotted sub-tables (cache.terminal_ttls, mcp.clients,
    # sources.memory) are freeform: their sub-keys are user-defined names.
    _KNOWN: dict[str, frozenset[str]] = {
        "paths": frozenset({"cache_db", "knowledge_db", "skill_dirs", "agent_home"}),
        "cache": frozenset(
            {
                "file_ttl",
                "file_ttls",
                "skill_ttl",
                "terminal_default_ttl",
                "terminal_ttls",
                "log_shell_fallback",
                "hash_algorithm",
                "max_memory_mb",
                "adaptive_ttl",
                "adaptive_factor",
                "adaptive_max_ttl",
                "stale_while_revalidate",
                "fuzzy_ttl_match",
                "fuzzy_threshold",
                "max_file_mb",
                "access_log_keep",
                "access_log_prune_interval",
                "api_cache_max_body_mb",
                "vacuum_min_free_mb",
            }
        ),
        "norm": frozenset({"enabled", "sort_lists", "strip_strings", "canonical_commands"}),
        "resilience": frozenset(
            {
                "coalescing",
                "coalescing_window",
                "retry",
                "retry_max_attempts",
                "retry_backoff_base",
                "circuit_breaker",
                "cb_failure_threshold",
                "cb_window",
                "cb_open_seconds",
            }
        ),
        "shim": frozenset({"exclude_prefixes", "tag_source", "stub_ttl", "stub_max_consecutive"}),
        "mcp": frozenset(
            {
                "allowed_paths",
                "allow_terminal",
                "allow_invalidate",
                "allowed_terminal_commands",
                "emit_context_hints",
                "clients",
                "tool_access_control",
                "dangerous_tool_keywords",
            }
        ),
        "mcp_multiplex": frozenset(
            {
                "enabled",
                "gateway",
                "gateway_exclude",
                "gateway_exclude_tools",
                "servers",
                "servers_config",
                "transparent_cache",
                "default_ttl",
                "idle_minutes",
                "fetch_revalidate",
            }
        ),
        "security": frozenset(
            {
                "cognitive_check_enabled",
                "ast_check_enabled",
                "dangerous_tool_keywords",
                "tool_access_control",
                "allow_symlink_escapes",
            }
        ),
        "tracker": frozenset({"ignore_read_prefixes", "persist_ttl"}),
        "storage": frozenset(
            {
                "backend",
                "db_path",
                "libsql_db",
                "sync_enabled",
                "sync_url",
                "sync_token",
                "sync_interval",
                "turso_api_base",
            }
        ),
        "recall": frozenset({"enabled", "ttl"}),
        # Shared network daemon (A6). network.clients is a freeform
        # sub-table: its keys are agent names mapped to [mcp.clients]
        # policy entries (A4 machinery), validated there.
        "network": frozenset(
            {
                "enabled",
                "bind",
                "port",
                "token",
                "tls",
                "tls_cert",
                "tls_key",
                "allow_public",
                "clients",
            }
        ),
        "docs": frozenset({"index_ttl"}),
        # "knowledge" is read verbatim by docs.py (_index_config_sources on
        # [[sources.knowledge]], documented in docs/KNOWLEDGE_DB.md) and was
        # missing from this list — so a user following the docs got a FALSE
        # "unknown key (key ignored)", and under -W error that escalated into
        # the whole config being discarded (2026-09-28).
        "sources": frozenset(
            {"scan_dirs", "scan_extensions", "scan_ignore", "max_file_kb", "memory", "knowledge"}
        ),
        "nginx": frozenset({"domain", "site_name", "ssl", "ssl_cert", "ssl_key", "github"}),
        # Emitted by toml_serializer.DEFAULT_CONFIG; port currently comes from
        # TOOLRECALL_FORWARD_PORT env (kept known so the section doesn't warn).
        "forward_proxy": frozenset(
            {"port", "extra_hosts", "dedup", "canonicalize_default", "bypass_sampled"}
        ),
        "update": frozenset({"enabled", "check_interval_hours", "index_url", "auto_apply"}),
    }
    # Sub-tables whose keys are user-defined names (per-client policies,
    # per-server configs, per-command TTL patterns) — never validated.
    _FREEFORM_KEYS: frozenset[str] = frozenset(
        {"terminal_ttls", "clients", "servers_config", "memory"}
    )
    # Per-ENTRY field schemas for freeform sub-tables whose entries are
    # consumed by code (review 2026-09): the parent section's key list is
    # the WRONG schema for entry fields — [network.clients."id"] token/
    # allowed_paths/private_writes and [mcp_multiplex.servers_config."srv"]
    # command/args are read verbatim by the daemon/multiplexer yet were
    # false-positived as "unknown key (key ignored)" against the parent's
    # keys. Keyed "<section>.<freeform>". Freeform tables absent here
    # (terminal_ttls: scalar values; sources.memory: user-defined store
    # names, schemaless today) get no entry-level validation.
    _FREEFORM_ENTRY_KEYS: dict[str, frozenset[str]] = {
        "network.clients": frozenset(
            {
                "token",
                "allowed_paths",
                "allow_terminal",
                "allowed_terminal_commands",
                "private_writes",
            }
        ),
        "mcp.clients": frozenset(
            {
                "emit_context_hints",
                "allowed_paths",
                "allow_terminal",
                "allowed_terminal_commands",
            }
        ),
        "mcp_multiplex.servers_config": frozenset({"command", "args", "env", "ttl"}),
    }

    def _warn_unknown_keys(self, known: dict, user: dict, source: str) -> None:
        """Warn when a user config defines sections/keys the schema doesn't know.

        Unknown sections are always reported. Within known sections, unknown
        keys are reported except in freeform sub-tables (per-client policies,
        per-server configs, terminal TTL patterns) where sub-keys are
        user-defined names. Typos then fail loudly instead of silently
        falling back to restrictive defaults.

        Every emission here is ADVISORY and can never abort the load. This
        method is called from INSIDE the try/except that discards the whole
        config on any exception, and under ``warnings.simplefilter("error")``
        (tests, ``-W error``, some CI) a plain ``warn()`` raises — so a config
        typo escalated into "CONFIG IGNORED": the entire file dropped,
        including ``[mcp] allowed_paths`` and ``[network] tokens``, leaving the
        instance silently on package defaults (2026-09-28). A diagnostic must
        not be able to do that.
        """
        import warnings

        def _advisory(msg: str) -> None:
            # Emitted under an explicit filter: catch_warnings means the
            # caller's error escalation cannot turn a config typo into a fatal
            # load, and it needs NO except-pass swallow to achieve that (the
            # swallow audit forbids silent ones). "default" keeps the
            # once-per-location dedup a plain warn() would have had — the
            # diagnostic still reaches the user, it just cannot abort the load.
            with warnings.catch_warnings():
                warnings.simplefilter("default")
                warnings.warn(msg)

        for section, sec_val in user.items():
            valid_keys = self._KNOWN.get(section)
            if valid_keys is None:
                _advisory(
                    f"ToolRecall: unknown config section [{section}] in {source} — "
                    f"typo? Known sections: {sorted(self._KNOWN)}"
                )
                continue
            if not isinstance(sec_val, dict):
                continue
            for key, key_val in sec_val.items():
                if key in valid_keys or key in self._FREEFORM_KEYS:
                    # Freeform sub-table: validate its ENTRIES' fields against
                    # the entry schema (NOT the parent section's key list —
                    # e.g. [network.clients."id"] token/allowed_paths are read
                    # verbatim by the daemon but are not [network] keys), so
                    # a typo'd entry field still fails loudly instead of
                    # silently falling back to restrictive defaults.
                    if key in self._FREEFORM_KEYS and isinstance(key_val, dict) and key_val:
                        entry_keys = self._FREEFORM_ENTRY_KEYS.get(f"{section}.{key}")
                        if entry_keys is not None:
                            for entry in key_val.values():
                                if not isinstance(entry, dict):
                                    continue
                                for sub in entry:
                                    if sub not in entry_keys:
                                        _advisory(
                                            f"ToolRecall: unknown key [{section}.{key}.*] {sub} "
                                            f"in {source} — typo? (field ignored)"
                                        )
                    continue
                _advisory(
                    f"ToolRecall: unknown config key [{section}] {key} in {source} — "
                    f"typo? (key ignored, default in effect)"
                )

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
    def libsql_db_path(self) -> str:
        """Path to libSQL database file. Defaults to a separate file from sqlite3 cache.db."""
        val = self.get("storage", "libsql_db", default=None)
        if val:
            return os.path.expanduser(val)
        # Separate file so switching back to sqlite3 doesn't corrupt cache
        base = os.path.dirname(os.path.expanduser(self.cache_db))
        return os.path.join(base, "cache-libsql.db")

    @property
    def libsql_sync_url(self) -> str | None:
        """Turso sync URL for libSQL embedded replica (optional)."""
        val = self.get("storage", "sync_url", default=None)
        return val.strip() if val else None

    @property
    def libsql_sync_token(self) -> str | None:
        """Turso auth token for libSQL sync (optional)."""
        val = self.get("storage", "sync_token", default=None)
        return val.strip() if val else None

    @property
    def libsql_sync_interval(self) -> int:
        """Seconds between libSQL sync rounds. 0 = disabled. Default: 60."""
        return int(self.get("storage", "sync_interval", default=60))

    @property
    def libsql_sync_enabled(self) -> bool:
        """Master switch for Turso Cloud sync. Default: False (opt-in).

        Sync replicates the entire cache (file contents, terminal output,
        MCP responses) to Turso Cloud, so it must be explicitly enabled --
        having sync_url/sync_token configured is NOT enough.
        Env override: TOOLRECALL_SYNC_ENABLED=true
        """
        val = self.get("storage", "sync_enabled", default=False)
        if isinstance(val, str):
            return val.strip().lower() in ("1", "true", "yes", "on")
        return bool(val)

    @property
    def recall_enabled(self) -> bool:
        """Enable the recall tier (lossless-recoverable eviction).

        Opt-in, default OFF — absent/present-but-false is disabled. Env
        override: TOOLRECALL_RECALL_ENABLED=true
        """
        val = self.get("recall", "enabled", default=False)
        if isinstance(val, str):
            return val.strip().lower() in ("1", "true", "yes", "on")
        return bool(val)

    @property
    def recall_ttl(self) -> float:
        """Seconds until a recalled block expires (0 = never expire).

        Default 0 (never expire) — preserves current behavior until the
        tier is enabled and a TTL is set. Env override: TOOLRECALL_RECALL_TTL
        """
        val = self.get("recall", "ttl", default=0)
        try:
            return float(val or 0)
        except (TypeError, ValueError):
            return 0.0

    @property
    def docs_index_ttl(self) -> float:
        """Seconds after which the FTS knowledge index is considered stale.

        Default 86400 (daily). 0 disables stale-triggered auto-refresh.
        Env override: TOOLRECALL_DOCS_INDEX_TTL
        """
        val = self.get("docs", "index_ttl", default=86400)
        try:
            return float(val)
        except (TypeError, ValueError):
            return 86400.0

    @property
    def turso_api_base(self) -> str:
        """Turso Platform API base URL. Customizable for self-hosted/proxy.
        Default: https://api.turso.tech
        """
        val = self.get("storage", "turso_api_base", default=None)
        # strip + rstrip("/"): a trailing slash in config would otherwise
        # produce "https://base//v1/..." URLs; empty string falls back too.
        return val.strip().rstrip("/") if val and val.strip() else "https://api.turso.tech"

    # ─── Network Daemon (A6) Properties ───────────────

    @property
    def network_enabled(self) -> bool:
        """Shared network daemon master switch. Default: False (opt-in).

        When False the daemon binds UDS only — behavior is byte-identical
        to pre-A6. Env override: TOOLRECALL_NETWORK_ENABLED=true
        """
        val = self.get("network", "enabled", default=False)
        if isinstance(val, str):
            return val.strip().lower() in ("1", "true", "yes", "on")
        return bool(val)

    @property
    def network_bind(self) -> str:
        """Bind address for the TCP listener. Default: 127.0.0.1 (loopback).

        A non-loopback bind exposes the cache to the network — only do this
        behind a VPN/SSH tunnel, and pair it with [network].tls=true.
        Env override: TOOLRECALL_NETWORK_BIND
        """
        return self.get("network", "bind", default="127.0.0.1")

    @property
    def network_port(self) -> int:
        """TCP port for the network daemon. Default: 8570.

        8568 = Windows IPC TCP, 8569 = forward proxy — 8570 keeps them apart.
        Env override: TOOLRECALL_NETWORK_PORT
        """
        try:
            return int(self.get("network", "port", default=8570))
        except (TypeError, ValueError):
            return 8570

    @property
    def network_token(self) -> str:
        """Pre-shared HMAC token for the network daemon. Default: "" (none).

        An empty token disables the listener even when enabled=true — a
        network daemon without auth must never start. Remote clients must
        present hmac_sha256(token, nonce) during the auth handshake.
        Env override: TOOLRECALL_NETWORK_TOKEN (preferred — keeps the
        secret out of toolrecall.toml; chmod 600 the file if you use it).
        """
        val = self.get("network", "token", default="")
        return val.strip() if isinstance(val, str) else ""

    @property
    def network_tls(self) -> bool:
        """TLS on the network listener. Default: False (opt-in).

        Off: HMAC auth only — fine on loopback or inside a VPN/SSH tunnel.
        On: listener speaks TLS (stdlib `ssl`, zero extra deps); requires
        tls_cert + tls_key. Clients enable via TOOLRECALL_NETWORK_TLS.
        Env override: TOOLRECALL_NETWORK_TLS
        """
        val = self.get("network", "tls", default=False)
        if isinstance(val, str):
            return val.strip().lower() in ("1", "true", "yes", "on")
        return bool(val)

    @property
    def network_allow_public(self) -> bool:
        """Allow binding a PUBLIC (non-local) address. Default: False.

        Local-networks-only by default (user requirement 2026-09-08):
        loopback, RFC1918 (10/8, 172.16/12, 192.168/16), link-local and
        IPv6 ULA bind fine; a public IP or 0.0.0.0 is refused with an
        explicit startup message. Set true ONLY when the daemon must be
        internet-reachable deliberately — pair with tls = true.
        Env override: TOOLRECALL_NETWORK_ALLOW_PUBLIC
        """
        val = self.get("network", "allow_public", default=False)
        if isinstance(val, str):
            return val.strip().lower() in ("1", "true", "yes", "on")
        return bool(val)

    @property
    def network_tls_cert(self) -> str | None:
        """PEM certificate chain path for [network].tls=true."""
        val = self.get("network", "tls_cert", default=None)
        return os.path.expanduser(val) if val else None

    @property
    def network_tls_key(self) -> str | None:
        """PEM private-key path for [network].tls=true. chmod 600."""
        val = self.get("network", "tls_key", default=None)
        return os.path.expanduser(val) if val else None

    @property
    def network_clients(self) -> dict:
        """Per-token-id agent policy for remote connections.

        Maps token-id → [mcp.clients] policy entry (A4 machinery). A
        token-id NOT listed here gets global-deny on remote: no allowed
        paths, no terminal, no invalidate — "compromised token = exactly
        the scoped policy, nothing more" requires explicit scoping.
        """
        raw = self.get("network", "clients", default={})
        return raw if isinstance(raw, dict) else {}

    @property
    def file_ttl(self) -> int:
        return self.get("cache", "file_ttl", default=-1)

    @property
    def max_file_mb(self) -> float:
        """Maximum file size the cache layer will read/cache, in MB.

        Default: 5 MB (~1.2M tokens). Rationale:
          - A 5MB text file already exceeds every mainstream context window,
            so content beyond it can never be *consumed* by the model — a
            cache "hit" on it would book savings that were never real context
            avoided, inflating reported metrics.
          - Giant rows re-inserted per turn were a diagnosed driver of
            cache-db slowdowns; the cap keeps transcripts/log-sized files
            out of SQLite while honest large-file caching happens below it.

        Configured via [cache].max_file_mb in toolrecall.toml or the
        TOOLRECALL_MAX_FILE_MB env var. Raise deliberately (e.g. for
        large-file benchmark arms), never as a blanket default change.
        """
        raw = self.get("cache", "max_file_mb", default=5)
        try:
            val = float(raw)
        except (TypeError, ValueError):
            return 5.0
        # Reject nonsense (0/negative disables caching entirely; absurd
        # values would OOM) — fall back to the documented default.
        if val <= 0 or val > 2048:
            return 5.0
        return val

    def api_cache_max_body_mb(self) -> float:
        """Maximum API response body the forward proxy will store, in MB.

        Default: 2 MB. The proxy tees *billed* LLM responses into api_cache; a
        body above this is skipped whole, never truncated — a truncated body
        would be replayed as a hit and hand the caller a corrupt response.

        Two separate problems, two separate levers: this cap bounds what one
        row can add, while the GC purge in cache.garbage_collect reclaims rows
        that expired (measured on the live DB: 12,281 of 12,385 rows were
        expired and held 1,103 MB of a 1,190 MB file, because api_cache was
        missing from the GC's table list; reclaiming them left an 86 MB DB).

        Configured via [cache].api_cache_max_body_mb; raise deliberately, never
        as a blanket default change.
        """
        raw = self.get("cache", "api_cache_max_body_mb", default=API_CACHE_MAX_BODY_MB_DEFAULT)
        try:
            val = float(raw)
        except (TypeError, ValueError):
            return API_CACHE_MAX_BODY_MB_DEFAULT
        # Reject nonsense (<=0 disables API caching entirely; absurd values
        # would let one row blow up the DB) — fall back to the documented default.
        if val <= 0 or val > 1024:
            return API_CACHE_MAX_BODY_MB_DEFAULT
        return val

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
        if isinstance(raw, str):
            raw = [raw]
        return [os.path.expanduser(p) for p in raw]

    @property
    def mcp_allow_terminal(self) -> bool:
        """Allow cached_terminal tool (default: False — security risk)."""
        return self.get("mcp", "allow_terminal", default=False)

    @property
    def mcp_allow_symlink_escapes(self) -> bool:
        """Permit reads through symlinks located INSIDE an allowed directory
        even when their target is not (path_utils Layer B, F1).

        Default False (review 2026-09): a stock install enforces strict
        realpath-only containment — no symlink inside an allowed dir can
        widen what is readable. This is the safe direction on Windows,
        where junction attacks against the Layer-B rescue are easiest.
        Distro/venv layouts that legitimately need the rescue
        (/etc/os-release → /usr/lib/os-release, site-packages links) opt
        in by setting this true (or TOOLRECALL_ALLOW_SYMLINK_ESCAPES=1).
        The sensitive-file blocklist applies either way.
        """
        val = self.get("security", "allow_symlink_escapes", default=False)
        if isinstance(val, bool):
            return val
        return str(val).strip().lower() not in ("0", "false", "no", "off")

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
    def mcp_require_validator(self) -> bool:
        """Refuse to serve MCP rows that carry no validator (default: False).

        A row with no ETag can never be revalidated against its origin, so the
        only remaining freshness policy is the clock. Measured on a mutating
        origin (Phase C, 240 shared requests): clock-based serving delivered
        ~29% wrong results including success<->error inversions, while
        validate-before-serve delivered 0%. Enabling this makes such rows
        report ``reason="unvalidated"`` instead of being served.

        Off by default so single-agent local caches (where the agent itself
        caused any change) keep working unchanged; on for shared caches.
        """
        return bool(self.get("mcp", "require_validator", default=False))

    @property
    def mcp_emit_context_hints(self) -> bool:
        """Emit context-hint text and stale-file markers after tool calls.

        Default: True (on). The ~60 B/tool-call overhead is negligible
        compared to the token savings from dropping clean files from context.
        Set False in append-only harnesses (Claude Code, Cursor) that
        cannot act on the hint.
        """
        return bool(self.get("mcp", "emit_context_hints", default=True))

    @property
    def mcp_client_hint_policy(self) -> dict:
        """Per-client hint policy (subset view of mcp_client_policy()).

        Kept for backward compatibility with existing callers/tests: maps
        client name → emit_context_hints bool, only for clients that set it.
        """
        return {
            name: entry["emit_context_hints"]
            for name, entry in self.mcp_client_policy().items()
            if "emit_context_hints" in entry
        }

    def mcp_client_policy(self) -> dict:
        """Full per-client policy table, keyed by MCP clientInfo.name.

        Recognized keys per client: emit_context_hints (bool),
        allowed_paths (list), allow_terminal (bool),
        allowed_terminal_commands (list). Unlisted keys are ignored with a
        config warning (unknown-key check in _load). Unlisted clients fall
        back to the global [mcp] settings.
        """
        clients = self.get("mcp", "clients", default={})
        if not isinstance(clients, dict):
            return {}
        policy: dict = {}
        for name, opts in clients.items():
            if isinstance(opts, dict):
                entry: dict = {}
                if "emit_context_hints" in opts:
                    entry["emit_context_hints"] = bool(opts["emit_context_hints"])
                if "allowed_paths" in opts:
                    entry["allowed_paths"] = list(opts["allowed_paths"] or [])
                if "allow_terminal" in opts:
                    entry["allow_terminal"] = bool(opts["allow_terminal"])
                if "allowed_terminal_commands" in opts:
                    entry["allowed_terminal_commands"] = list(
                        opts["allowed_terminal_commands"] or []
                    )
                if entry:
                    policy[name] = entry
        return policy

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
        default_keywords = [
            "write",
            "edit",
            "delete",
            "remove",
            "terminal",
            "bash",
            "exec",
            "run",
            "push",
            "commit",
            "update",
            "create",
        ]
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
    def mcp_multiplex_gateway(self) -> bool:
        """Expose multiplexed servers' tools as first-class bridge tools.

        When true the bridge advertises each configured server's tools in
        ``tools/list`` as ``mcp__<server>__<tool>`` and routes those calls
        through the same daemon ``mcp_call`` path. Default OFF: it changes
        every client's tool surface, so it is opt-in.

        Why it exists: the `mcp_call`-only surface measured ZERO organic
        traffic (2026-09-26) — an agent must list servers, learn the names,
        then take two indirections; nothing does that spontaneously.
        """
        return bool(self.get("mcp_multiplex", "gateway", default=False))

    @property
    def mcp_multiplex_gateway_exclude(self) -> list:
        """Server names the gateway must NOT advertise (default: none).

        Surface control, not enforcement: a hidden server's tools stay callable
        through ``mcp_call`` (and by an agent that already knows the name).
        Use ``security.tool_access_control`` when the goal is to BLOCK.
        """
        return self.get("mcp_multiplex", "gateway_exclude", default=[])

    @property
    def mcp_multiplex_gateway_exclude_tools(self) -> list:
        """Tool names the gateway must NOT advertise, across all servers.

        Matched case-insensitively, so ``["write_query", "create_table"]``
        hides those names everywhere while the read tools stay visible — the
        usual intent when a server points at a live database.
        """
        return self.get("mcp_multiplex", "gateway_exclude_tools", default=[])

    @property
    def mcp_multiplex_fetch_revalidate(self) -> bool:
        """ETag revalidation for the fetch server (opt-in, default OFF).

        When true, a transparent-cache MISS with an expired-but-validated
        row triggers one If-None-Match request: 304 keeps the old body (no
        body transfer), 200 replaces it. Requires the row to have an etag
        captured on the original store (see fetch_revalidate tool).
        """
        return self.get("mcp_multiplex", "fetch_revalidate", default=False)

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

    @staticmethod
    def _mcp_registry_command(name_lower: str) -> tuple[str, list[str]] | None:
        """Registry command/args for a server name; built-ins run under the
        active interpreter (same substitution as the auto-resolve path)."""
        resolved = _resolve_mcp_server(name_lower)
        if resolved is None:
            return None
        cmd, args, source = resolved
        if source == "builtin":
            return sys.executable, list(args)
        return cmd, list(args)

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
        # A partial override (e.g. only `ttl` or `env`) must NOT erase the
        # registry's command/args — the auto-resolved command is what makes
        # the server runnable. Found live 2026-09-22: `fetch = { ttl = 300 }`
        # produced command="" → "Permission denied: ''" on every fetch call.
        # Merge: user keys win, registry command/args fill the gaps.
        for _name, _entry in list(result.items()):
            if isinstance(_entry, dict) and "command" not in _entry:
                _pair = self._mcp_registry_command(str(_name).lower())
                if _pair is not None:
                    _entry.setdefault("command", _pair[0])
                    _entry.setdefault("args", _pair[1])
                else:
                    # Unknown name without a command can never start.
                    _entry.setdefault("command", "")
        result = {k: v for k, v in result.items() if isinstance(v, dict) and v.get("command")}

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

        Default: /proc, /sys, /dev (kernel virtual filesystems — generated
        live, so they can never produce a cache hit) plus the resolved agent
        home (control-plane files such as config.yaml, cron/jobs.json and plugin
        manifests, which never enter an LLM context). The security gate still
        applies to anything a *tool* reads via the daemon's cached_read — this
        list only governs the in-process shim.

        Empty list = bypass NOTHING (all open() calls go through the shim).
        """
        raw = self.get(
            "shim",
            "exclude_prefixes",
            default=None,
        )
        if raw is None:
            return self._default_shim_exclude_prefixes()
        if isinstance(raw, list):
            return raw
        return []

    def _default_shim_exclude_prefixes(self) -> list:
        """Kernel virtual filesystems plus the agent home.

        Agent-home reads are control plane: the shim already tags them
        ``source="shim"`` via ``shim._LIBRARY_DIR_MARKERS``, so they never count
        toward ``tokens_saved_to_llm`` — yet they still paid a daemon
        round-trip and an ``access_log`` row. On 2026-11-09 that was 25,952 of
        26,076 reads (99.5 %), driven by cron workers reloading their own
        manifests.

        Resolved through :attr:`agent_home` (AGENT_HOME →
        TOOLRECALL_AGENT_HOME → ``[paths].agent_home`` → ``~/.hermes``), so no
        agent layout is hardcoded in the decision. A bare ``/`` is never added:
        it would skip every path and disable the shim.
        """
        prefixes = ["/proc/", "/sys/", "/dev/"]
        home = (self.agent_home or "").rstrip("/")
        if home:
            prefixes.append(home + "/")
        return prefixes

    @property
    def shim_stub_ttl(self) -> float:
        """TTL (seconds) for the shim's unchanged-file stub, Lever 1.

        WHAT THIS TTL IS — and what it is NOT:

        It does NOT guard against stale disk content. Every shim cache hit
        is already re-validated against the file's mtime+size before it is
        served, so a stub can never reference different bytes than the disk
        holds. The TTL guards the AGENT-memory axis: a stub works by
        claiming "the agent already holds this content from an earlier
        read". Agent harnesses compact old tool results out of the context
        window; after compaction the stub references content the agent no
        longer has, which strands it for a recovery turn. The TTL bounds
        how old a "you already hold this" claim may be before ToolRecall
        stops trusting it and re-sends the full content.

        In short: mtime+size proves the DISK is current; the TTL bounds how
        much we trust the AGENT's memory. They are independent validators
        and both are required (disabling the TTL re-opens the
        read-then-compact-then-re-read stranding case).

        Tuning: higher = more measured savings, higher stranding risk after
        harness compactions; lower = more duplicate re-sends. The optimal
        value tracks the harness's compaction cadence — start at the 300s
        default and adjust from observed behavior. ``0`` disables stubbing
        entirely (cache still serves, just without the stub).

        Configured via [shim].stub_ttl in toolrecall.toml, or
        TOOLRECALL_SHIM_STUB_TTL env var (seconds). The MCP bridge stub
        deliberately keeps its own shorter TTL (60s) — append-only harnesses
        like Claude Code compact aggressively.
        """
        val = self.get("shim", "stub_ttl", default=300)
        try:
            v = float(val)
            return v if v >= 0 else 300.0
        except (TypeError, ValueError):
            return 300.0

    @property
    def shim_stub_max_consecutive(self) -> int:
        """Max consecutive stubs per path before forcing a full send (compaction armor). Default: 5.

        Configured via [shim].stub_max_consecutive in toolrecall.toml, or
        TOOLRECALL_SHIM_STUB_MAX_CONSECUTIVE env var.
        """
        val = self.get("shim", "stub_max_consecutive", default=5)
        try:
            v = int(val)
            return max(0, v)
        except (TypeError, ValueError):
            return 5

    # ─── Context Tracker Properties ─────────────────────

    @property
    def tracker_persist_ttl(self) -> float:
        """How long a persisted tracker snapshot stays restorable, in seconds.

        Configured via [tracker].persist_ttl in toolrecall.toml, or
        TOOLRECALL_TRACKER_PERSIST_TTL env var (seconds). Default 21600
        (6 hours): long enough to survive a daemon restart / crash loop
        within one working day, short enough that a read set never
        describes a previous DAY's session — restoring yesterday's
        read set would report files as "clean in the agent's context"
        that the agent has not seen today (wrong drops).
        0 disables restore entirely (snapshot still written, state simply
        not read back — old behavior).

        Read-set mtimes are re-validated on restore regardless of TTL:
        a file modified after the snapshot is dropped from the read set.
        """
        val = self.get("tracker", "persist_ttl", default=21600)
        try:
            return float(val)
        except (TypeError, ValueError):
            return 21600.0

    @property
    def tracker_ignore_read_prefixes(self) -> list:
        """Path prefixes NEVER tracked as agent reads by the context tracker.

        Files matching these prefixes are counted by the file cache but are
        NOT added to the tracker's clean set. Daemon-side serve-through reads
        (agent session transcripts like ~/.hermes/webui/sessions/*.json, agent
        config/cache files) are never present in any agent context — tracking
        them poisoned the clean set and inflated potential-savings figures
        (~380MB of session JSONs observed on 2026-09-10).

        Configured via [tracker].ignore_read_prefixes in toolrecall.toml,
        or TOOLRECALL_TRACKER_IGNORE_PREFIXES env var (comma-separated).

        Default: agent session transcript dirs + the agent home data dirs
        (~/.hermes/webui, ~/.hermes/sessions, ~/.hermes/cache, plus any
        */webui/sessions path) + ToolRecall's own state dir (~/.toolrecall —
        the daemon/TUI/healthcheck read its own CSVs/DBs serve-through; that
        data is never in an agent context). Empty list = track everything
        (old behavior).
        """
        default = [
            "~/.hermes/webui/sessions/",
            "~/.hermes/webui/sessions.json",
            "~/.hermes/sessions/",
            "~/.hermes/cache/",
            "~/.toolrecall/",
            "*/webui/sessions/",
        ]
        raw = self.get(
            "tracker",
            "ignore_read_prefixes",
            default=default,
        )
        if isinstance(raw, list):
            prefixes = [str(p) for p in raw]
        else:
            # Config loader guarantees list (ENV_MAP _LIST_KEYS); a raw str
            # from a hand-edited TOML would otherwise silently disable the
            # entire ignore list.
            prefixes = [str(raw)] if str(raw).strip() else default
        # The tempdir is ephemeral working space (pytest tmp_path, e2e fixture
        # trees), never agent-session infra. When TMPDIR nests inside an
        # ignored prefix (Hermes scratch: TMPDIR=~/.hermes/cache/scratch under
        # ~/.hermes/cache/), every test/e2e read is silently untracked and
        # context_get_stats reads 0 — drop prefixes that contain gettempdir().
        try:
            import tempfile as _tempfile

            tmp = os.path.realpath(_tempfile.gettempdir())
            prefixes = [
                p for p in prefixes if not tmp.startswith(os.path.realpath(os.path.expanduser(p)))
            ]
        except (OSError, ValueError):
            pass
        return prefixes

    # ─── Update (auto-updater) Properties ───────────────

    @property
    def update_enabled(self) -> bool:
        """Auto-update check at CLI entry (default: True — opt OUT to disable).

        Configured via [update].enabled in toolrecall.toml.
        TOOLRECALL_UPDATE_CHECK env var overrides (0/false/no/off = disabled),
        matching the global env > TOML priority. Single source of truth:
        updater._update_enabled() delegates here.
        """
        env = os.environ.get("TOOLRECALL_UPDATE_CHECK")
        if env is not None:
            return env.strip().lower() not in ("0", "false", "no", "off")
        return bool(self.get("update", "enabled", default=True))

    @property
    def update_index_url(self) -> str | None:
        """Explicit package index for the auto-updater (F2 supply-chain pin).

        When set, upgrades are pinned to this URL; otherwise the env var
        TOOLRECALL_UPDATE_INDEX_URL applies; otherwise the official PyPI.
        Configured via [update].index_url in toolrecall.toml.
        """
        val = self.get("update", "index_url", default=None)
        return str(val).strip() if val else None

    @property
    def update_auto_apply(self) -> bool:
        """Install a newer release automatically when the CLI hook detects it.

        Default FALSE (review 2026-09): a compromised PyPI account must not
        mean silent code execution on every user's next CLI run. When False,
        the CLI hook prints the available version and the agent-facing hint
        channel announces it (updater.pending_update_hint) — installing is an
        explicit `toolrecall update`.
        TOOLRECALL_UPDATE_AUTO_APPLY env wins (matching env > TOML priority).
        """
        env = os.environ.get("TOOLRECALL_UPDATE_AUTO_APPLY")
        if env is not None:
            return env.strip().lower() in ("1", "true", "yes", "on")
        return bool(self.get("update", "auto_apply", default=False))

    @property
    def update_check_interval_hours(self) -> float:
        """Minimum hours between PyPI version checks (default: 24).

        A failed check still counts as checked — no hot-loop retries offline.
        Configured via [update].check_interval_hours in toolrecall.toml.
        """
        raw = self.get("update", "check_interval_hours", default=24)
        try:
            return max(float(raw), 0.0)
        except (TypeError, ValueError):
            return 24.0


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


def load_config(path: str | None = None) -> Config:
    """Load ToolRecall configuration. Returns a fresh Config instance each call.

    Unlike the previous singleton pattern, this always creates a new Config
    so that TOOLRECALL_* environment variables set after the first import
    are respected. Callers that want caching should store the result.
    """
    return Config(path)
