"""ToolRecall Turso Cloud integration — `tr turso <cmd>` CLI commands.

Provides:
  - `tr turso init`     — Setup: create Turso DB, generate sync token, write config.
                          Writes sync_enabled=false unless explicitly confirmed.
  - `tr turso enable`   — Flip sync_enabled=true in config
  - `tr turso disable`  — Flip sync_enabled=false in config
  - `tr turso status`   — Show sync configuration

The whole integration is OPTIONAL and OFF by default:
  - It only matters when storage.backend = "libsql".
  - Even then, sync runs only if storage.sync_enabled = true (default false).
  - The Turso Platform API base URL is customizable via
    [storage].turso_api_base or TOOLRECALL_TURSO_API_BASE (self-hosted setups).

⚠️  Enabling sync replicates the ENTIRE cache — cached file contents,
terminal stdout/stderr, MCP responses — to Turso Cloud.

Uses the Turso Platform REST API directly (no Turso CLI dependency).
"""

import os
import re
import sys
import json
import getpass
import urllib.request
import urllib.error


def _api_base() -> str:
    """Turso Platform API base — customizable via config or env."""
    env = os.environ.get("TOOLRECALL_TURSO_API_BASE")
    if env:
        return env.rstrip("/")
    try:
        from toolrecall.config import load_config
        return load_config().turso_api_base
    except Exception:
        return "https://api.turso.tech"


def _api_req(method: str, path: str, token: str, body: dict | None = None) -> dict:
    """Make an authenticated request to the Turso Platform API."""
    url = f"{_api_base()}{path}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "User-Agent": "toolrecall-turso/0.2",
    }
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        detail = e.read().decode() if e.fp else ""
        raise RuntimeError(f"Turso API error {e.code}: {detail}") from e


def _prompt(label: str, default: str = "", secret: bool = False) -> str:
    """Prompt for input. Env override: TURSO_<LABEL>. Secrets use getpass (no echo)."""
    env_key = f"TURSO_{label.upper().replace(' ', '_')}"
    val = os.environ.get(env_key)
    if val:
        return val

    hint = f" [{default}]" if default else ""
    try:
        if secret:
            val = getpass.getpass(f"  {label}{hint} (input hidden): ").strip()
        else:
            print(f"  {label}{hint}: ", end="", flush=True)
            val = sys.stdin.readline().strip()
    except (EOFError, KeyboardInterrupt):
        print()
        sys.exit(1)

    return val or default


def _confirm(label: str, default_no: bool = True) -> bool:
    """Yes/no prompt, defaulting to No. Env override: TURSO_<LABEL>=yes."""
    env_key = f"TURSO_{label.upper().replace(' ', '_').replace('?', '')}"
    env = os.environ.get(env_key)
    if env is not None:
        return env.strip().lower() in ("1", "true", "yes", "y", "on")
    suffix = " [y/N]: " if default_no else " [Y/n]: "
    print(f"  {label}{suffix}", end="", flush=True)
    try:
        ans = sys.stdin.readline().strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    if not ans:
        return not default_no
    return ans in ("y", "yes")


def _hostname_from(db: dict) -> str:
    """Turso API responses vary in key casing across versions."""
    return db.get("Hostname") or db.get("hostname") or ""


def _write_private(path: str, content: str) -> None:
    """Write a file containing secrets with 0600 perms (owner-only)."""
    # Create/truncate with restrictive mode from the start — chmod-after-write
    # would leave a world-readable window.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(content)
    os.chmod(path, 0o600)  # tighten pre-existing files too


def _upsert_env_lines(existing: str, updates: dict[str, str]) -> str:
    """Replace TOOLRECALL_SYNC_* lines in-place; append missing ones.

    Unlike the previous append-only behavior, re-running init does not
    accumulate stale (possibly revoked) tokens in the file.
    """
    lines = existing.split("\n") if existing else []
    seen = set()
    out = []
    for line in lines:
        key = line.split("=", 1)[0].strip() if "=" in line else None
        if key in updates:
            out.append(f"{key}={updates[key]}")
            seen.add(key)
        else:
            out.append(line)
    for key, val in updates.items():
        if key not in seen:
            out.append(f"{key}={val}")
    return "\n".join(out).rstrip() + "\n"


def cmd_turso_init():
    """Setup: create Turso DB, generate sync token, write config (sync stays OFF unless confirmed)."""
    print("=" * 56)
    print("  ToolRecall — Turso Cloud Init")
    print("=" * 56)
    print()
    print("  This creates a Turso database and writes sync settings for")
    print("  the libSQL backend. You need a Turso Platform API token.")
    print()
    print("  Get one: https://docs.turso.tech/api-reference/authentication")
    print()
    print("  ⚠️  If you later ENABLE sync, the entire cache (cached file")
    print("      contents, terminal output, MCP responses) is replicated")
    print("      to Turso Cloud. Sync stays DISABLED unless you opt in.")
    print()

    # ─── Gather credentials ───────────────────────────────
    org_slug = _prompt("Organization slug", default=os.environ.get("TURSO_ORG", ""))
    api_token = _prompt("Platform API token", secret=True)

    if not org_slug or not api_token:
        print("  ❌ Organization slug and API token are required.")
        sys.exit(1)

    print("  🔍 Validating credentials...")
    try:
        _api_req("GET", f"/v1/organizations/{org_slug}", api_token)
    except RuntimeError as e:
        print(f"  ❌ Credentials invalid: {e}")
        sys.exit(1)
    print("  ✅ Credentials valid.")

    # ─── Create database ──────────────────────────────────
    db_name = _prompt("Database name", default=f"toolrecall-{org_slug}")
    group = _prompt("Group name", default="default")

    print(f"  🗄️  Creating database '{db_name}' in group '{group}'...")
    hostname = ""
    try:
        result = _api_req(
            "POST",
            f"/v1/organizations/{org_slug}/databases",
            api_token,
            body={"name": db_name, "group": group},
        )
        hostname = _hostname_from(result.get("database", {}))
    except RuntimeError as e:
        if "409" in str(e):
            print(f"  ℹ️  Database '{db_name}' already exists — reusing it.")
        else:
            print(f"  ❌ Failed to create database: {e}")
            sys.exit(1)

    if not hostname:
        # Existing DB (409) or creation response without hostname — fetch details.
        try:
            detail = _api_req(
                "GET",
                f"/v1/organizations/{org_slug}/databases/{db_name}",
                api_token,
            )
            hostname = _hostname_from(detail.get("database", {}))
        except RuntimeError as e:
            print(f"  ❌ Could not retrieve database details: {e}")
            sys.exit(1)
    if not hostname:
        print("  ❌ Turso API returned no hostname for the database.")
        sys.exit(1)

    sync_url = f"libsql://{hostname}"
    print(f"  ✅ Database ready — sync URL: {sync_url}")

    # ─── Generate auth token (customizable scope/lifetime) ─
    # Defaults are deliberately conservative: expiring, read-write on this
    # DB only. Override via prompt or TURSO_TOKEN_EXPIRATION /
    # TURSO_TOKEN_AUTHORIZATION for other policies (e.g. "never").
    expiration = _prompt("Token expiration (e.g. 30d, 2w, never)", default="30d")
    if not re.fullmatch(r"never|\d+[smhdw]", expiration):
        print(f"  ❌ Invalid expiration '{expiration}' (expected e.g. 30d, 2w, never).")
        sys.exit(1)
    authorization = _prompt("Token authorization (full-access | read-only)", default="full-access")
    if authorization not in ("full-access", "read-only"):
        print(f"  ❌ Invalid authorization '{authorization}'.")
        sys.exit(1)
    if expiration == "never":
        print("  ⚠️  Non-expiring token: if it leaks, access is permanent until revoked.")

    print("  🔑 Generating database auth token...")
    try:
        token_result = _api_req(
            "POST",
            f"/v1/organizations/{org_slug}/databases/{db_name}/auth/tokens"
            f"?expiration={expiration}&authorization={authorization}",
            api_token,
        )
    except RuntimeError as e:
        print(f"  ❌ Failed to generate token: {e}")
        sys.exit(1)

    db_token = token_result.get("jwt", "")
    if not db_token:
        print("  ❌ Token generation returned empty JWT.")
        sys.exit(1)
    print(f"  ✅ Token generated ({authorization}, expires: {expiration}).")

    # ─── Opt-in decision (default: NO) ────────────────────
    enable_now = _confirm("Enable sync now (uploads cache contents to Turso Cloud)?")

    # ─── Write config (0600) ──────────────────────────────
    cfg_dir = os.path.expanduser("~/.config/toolrecall")
    cfg_path = os.path.join(cfg_dir, "toolrecall.toml")
    os.makedirs(cfg_dir, exist_ok=True)

    existing = ""
    if os.path.exists(cfg_path):
        with open(cfg_path) as f:
            existing = f.read()

    api_base = _api_base()
    storage_lines = [
        "[storage]",
        'backend = "libsql"',
        f"sync_enabled = {'true' if enable_now else 'false'}  # master switch — sync is opt-in",
        f'sync_url = "{sync_url}"',
        f'sync_token = "{db_token}"',
        "sync_interval = 60  # seconds between syncs",
    ]
    if api_base != "https://api.turso.tech":
        storage_lines.append(f'turso_api_base = "{api_base}"')
    storage_block = "\n".join(storage_lines) + "\n"

    if "[storage]" in existing:
        lines = existing.split("\n")
        new_lines = []
        in_storage = False
        storage_done = False
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("[") and stripped != "[storage]" and in_storage:
                in_storage = False
            if stripped == "[storage]":
                in_storage = True
                if not storage_done:
                    new_lines.append(storage_block.rstrip())
                    storage_done = True
                continue
            if in_storage:
                continue  # drop old storage lines
            new_lines.append(line)
        if not storage_done:
            new_lines.append("")
            new_lines.append(storage_block.rstrip())
        existing = "\n".join(new_lines)
    else:
        existing = (existing.rstrip() + "\n\n" if existing.strip() else "") + storage_block

    _write_private(cfg_path, existing)

    # ─── .env: upsert, never append duplicates, 0600 ──────
    env_path = os.path.join(cfg_dir, ".env")
    env_existing = ""
    if os.path.exists(env_path):
        with open(env_path) as f:
            env_existing = f.read()
    env_content = _upsert_env_lines(env_existing, {
        "TOOLRECALL_SYNC_ENABLED": "true" if enable_now else "false",
        "TOOLRECALL_SYNC_URL": sync_url,
        "TOOLRECALL_SYNC_TOKEN": db_token,
        "TOOLRECALL_SYNC_INTERVAL": "60",
    })
    _write_private(env_path, env_content)

    print()
    print("  ✅ Config written (permissions 0600):")
    print(f"     {cfg_path}")
    print(f"     {env_path}")
    print()
    if enable_now:
        print("  🔄 Sync is ENABLED. Restart the daemon to apply:")
    else:
        print("  ⏸️  Sync is DISABLED (default). To enable later:")
        print("     toolrecall turso enable")
        print("  Then restart the daemon:")
    print("     toolrecall daemon restart")
    print("=" * 56)


def _set_sync_enabled(value: bool) -> None:
    """Flip sync_enabled in ~/.config/toolrecall/toolrecall.toml."""
    cfg_path = os.path.expanduser("~/.config/toolrecall/toolrecall.toml")
    if not os.path.exists(cfg_path):
        print(f"  ❌ No config at {cfg_path} — run 'toolrecall turso init' first.")
        sys.exit(1)
    with open(cfg_path) as f:
        content = f.read()
    flag = "true" if value else "false"
    if re.search(r"(?m)^\s*sync_enabled\s*=", content):
        content = re.sub(
            r"(?m)^(\s*)sync_enabled\s*=\s*\S+(.*)$",
            rf"\g<1>sync_enabled = {flag}\g<2>",
            content, count=1)
    elif "[storage]" in content:
        content = content.replace("[storage]", f"[storage]\nsync_enabled = {flag}", 1)
    else:
        content = content.rstrip() + f"\n\n[storage]\nsync_enabled = {flag}\n"
    _write_private(cfg_path, content)
    state = "ENABLED — cache contents will replicate to Turso Cloud" if value else "disabled"
    print(f"  ✅ sync_enabled = {flag} ({state}).")
    print("     Restart the daemon to apply: toolrecall daemon restart")


def cmd_turso_status():
    """Show Turso sync status."""
    from toolrecall.cache import get_stats

    stats = get_stats()
    backend = stats.get("storage_backend", "sqlite")
    if backend != "libsql":
        print("  ℹ️  libSQL backend is not active — Turso sync does not apply.")
        print("     Set [storage].backend = \"libsql\" and run 'toolrecall turso init'.")
        return

    enabled = stats.get("sync_enabled", False)
    sync_url = stats.get("sync_url") or "not configured"
    sync_interval = stats.get("sync_interval", "?")

    print("  ── Turso Sync Status ──")
    print(f"  Backend:        {backend}")
    print(f"  Sync enabled:   {'yes' if enabled else 'no (opt-in — toolrecall turso enable)'}")
    print(f"  Sync URL:       {sync_url}")
    print(f"  Sync interval:  {sync_interval}s")


def cmd_turso(args: list[str]):
    """Dispatch turso subcommands: init, enable, disable, status."""
    if not args or args[0] in ("--help", "-h"):
        print("Usage: toolrecall turso <subcommand>")
        print()
        print("Subcommands:")
        print("  init       Set up Turso Cloud (create DB, generate token, write config)")
        print("             Sync remains DISABLED unless explicitly confirmed.")
        print("  enable     Turn sync on  (sync_enabled = true)")
        print("  disable    Turn sync off (sync_enabled = false)")
        print("  status     Show sync status")
        return

    sub = args[0]
    if sub == "init":
        cmd_turso_init()
    elif sub == "enable":
        _set_sync_enabled(True)
    elif sub == "disable":
        _set_sync_enabled(False)
    elif sub == "status":
        cmd_turso_status()
    else:
        print(f"Unknown turso subcommand: {sub}")
        print("Available: init, enable, disable, status")
