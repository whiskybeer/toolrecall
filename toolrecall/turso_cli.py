"""ToolRecall Turso Cloud integration — `tr turso init/status` CLI commands.

Provides:
  - `tr turso init`   — Interactive setup: create Turso DB, generate sync token, write config
  - `tr turso status` — Show sync status from daemon (when backend=libsql)

Uses the Turso Platform REST API directly (no Turso CLI dependency).
"""

import os
import sys
import json
import urllib.request
import urllib.error
import urllib.parse

# ─── API constants ──────────────────────────────────────────────
_TURSO_API_BASE = "https://api.turso.tech"


def _api_req(method: str, path: str, token: str, body: dict | None = None) -> dict:
    """Make an authenticated request to the Turso Platform API."""
    url = f"{_TURSO_API_BASE}{path}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "User-Agent": "toolrecall-turso/0.1",
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
    """Prompt the user for input. Falls back to reading from env if set."""
    # Check env override first
    env_key = f"TURSO_{label.upper().replace(' ', '_')}"
    val = os.environ.get(env_key)
    if val:
        return val

    hint = f" [{default}]" if default else ""
    secret_hint = " (input hidden)" if secret else ""
    print(f"  {label}{hint}{secret_hint}: ", end="", flush=True)
    try:
        if secret:
            # Fallback: read stdin (no echo control in pure stdlib)
            val = sys.stdin.readline().strip()
            print()
        else:
            val = sys.stdin.readline().strip()
    except (EOFError, KeyboardInterrupt):
        print()
        sys.exit(1)

    if not val and default:
        return default
    return val


def cmd_turso_init():
    """Interactive setup: create Turso DB, generate sync token, write config."""
    print("=" * 56)
    print("  ToolRecall — Turso Cloud Init")
    print("=" * 56)
    print()
    print("  This will create a Turso database and configure sync for")
    print("  your libSQL backend. You need a Turso Platform API token.")
    print()
    print("  Get one: https://docs.turso.tech/api-reference/authentication")
    print()

    # ─── Gather credentials ───────────────────────────────
    org_slug = _prompt("Organization slug", default=os.environ.get("TURSO_ORG", ""))
    api_token = _prompt("Platform API token", secret=True)

    if not org_slug or not api_token:
        print("  ❌ Organization slug and API token are required.")
        sys.exit(1)

    # Validate token by calling whoami
    print("  🔍 Validating credentials...")
    try:
        _api_req("GET", f"/v1/organizations/{org_slug}", api_token)
    except RuntimeError as e:
        print(f"  ❌ Credentials invalid: {e}")
        sys.exit(1)
    print("  ✅ Credentials valid.")

    # ─── Create database ──────────────────────────────────
    db_name = _prompt(
        "Database name",
        default=f"toolrecall-{org_slug}",
    )
    group = _prompt("Group name", default="default")

    print(f"  🗄️  Creating database '{db_name}' in group '{group}'...")
    try:
        result = _api_req(
            "POST",
            f"/v1/organizations/{org_slug}/databases",
            api_token,
            body={"name": db_name, "group": group},
        )
    except RuntimeError as e:
        if "409" in str(e):
            print(f"  ℹ️  Database '{db_name}' already exists — continuing.")
        else:
            print(f"  ❌ Failed to create database: {e}")
            sys.exit(1)

    db = result.get("database", {})
    hostname = db.get("Hostname", "")
    if not hostname:
        # Fetch existing DB details
        try:
            detail = _api_req(
                "GET",
                f"/v1/organizations/{org_slug}/databases/{db_name}",
                api_token,
            )
            hostname = detail.get("database", {}).get("Hostname", "")
        except RuntimeError as e:
            print(f"  ❌ Could not retrieve database details: {e}")
            sys.exit(1)

    sync_url = f"libsql://{hostname}"
    print(f"  ✅ Database ready — sync URL: {sync_url}")

    # ─── Generate auth token ──────────────────────────────
    print("  🔑 Generating database auth token...")
    try:
        token_result = _api_req(
            "POST",
            f"/v1/organizations/{org_slug}/databases/{db_name}/auth/tokens?expiration=never&authorization=full-access",
            api_token,
        )
    except RuntimeError as e:
        print(f"  ❌ Failed to generate token: {e}")
        sys.exit(1)

    db_token = token_result.get("jwt", "")
    if not db_token:
        print("  ❌ Token generation returned empty JWT.")
        sys.exit(1)
    print("  ✅ Token generated.")

    # ─── Write config ─────────────────────────────────────
    cfg_dir = os.path.expanduser("~/.config/toolrecall")
    cfg_path = os.path.join(cfg_dir, "toolrecall.toml")
    os.makedirs(cfg_dir, exist_ok=True)

    # Read existing config
    existing = ""
    if os.path.exists(cfg_path):
        with open(cfg_path) as f:
            existing = f.read()

    # Inject or update [storage] section
    storage_lines = [
        "[storage]",
        f'backend = "libsql"',
        f'sync_url = "{sync_url}"',
        f'sync_token = "{db_token}"',
        "sync_interval = 60  # seconds between syncs",
    ]
    storage_block = "\n".join(storage_lines) + "\n"

    if "[storage]" in existing:
        # Replace existing [storage] block
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
                continue  # skip old storage lines
            new_lines.append(line)
        # If [storage] wasn't found, append
        if not storage_done:
            new_lines.append("")
            new_lines.append(storage_block.rstrip())
        existing = "\n".join(new_lines)
    else:
        existing = existing.rstrip() + "\n\n" + storage_block

    with open(cfg_path, "w") as f:
        f.write(existing)

    # Also write .env with the sync token for reference
    env_path = os.path.join(cfg_dir, ".env")
    env_lines = [
        f"# Turso Cloud sync — created by `toolrecall turso init`",
        f"TOOLRECALL_SYNC_URL={sync_url}",
        f"TOOLRECALL_SYNC_TOKEN={db_token}",
        f"TOOLRECALL_SYNC_INTERVAL=60",
        "",
    ]
    with open(env_path, "a") as f:
        f.write("\n".join(env_lines))

    print()
    print("  ✅ Config written to:")
    print(f"     {cfg_path}")
    print(f"     {env_path}")
    print()
    print("  ℹ️  Restart the daemon to apply changes:")
    print("     toolrecall restart")
    print("=" * 56)


def cmd_turso_status():
    """Show Turso sync status from the daemon."""
    from toolrecall.cache import get_stats

    stats = get_stats()

    # Check if libSQL backend is active
    backend = stats.get("storage_backend", "sqlite")
    if backend != "libsql":
        print("  ℹ️  libSQL backend is not active — Turso sync not configured.")
        print("     Run 'toolrecall turso init' to set up Turso Cloud sync.")
        return

    sync_url = stats.get("sync_url", "not configured")
    sync_interval = stats.get("sync_interval", "?")

    print("  ── Turso Sync Status ──")
    print(f"  Backend:        {backend}")
    print(f"  Sync URL:       {sync_url}")
    print(f"  Sync interval:  {sync_interval}s")


def cmd_turso(args: list[str]):
    """Dispatch turso subcommands: init, status."""
    if not args or args[0] in ("--help", "-h"):
        print("Usage: toolrecall turso <subcommand>")
        print()
        print("Subcommands:")
        print("  init       Interactive Turso Cloud setup (create DB, generate token, write config)")
        print("  status     Show sync status")
        return

    sub = args[0]
    if sub == "init":
        cmd_turso_init()
    elif sub == "status":
        cmd_turso_status()
    else:
        print(f"Unknown turso subcommand: {sub}")
        print("Available: init, status")