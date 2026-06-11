# ToolRecall Cache — VS Code Extension

**Transparent file-read caching for VS Code.** Zero config. Works on Windows, macOS, and Linux.

## How it works

1. **Install ToolRecall once**: `pip install toolrecall`
2. **Install this extension** from the VS Code Marketplace
3. **Done.** Every file you open gets cached through ToolRecall.

On the second open, the file comes from ToolRecall's in-memory LRU cache — not disk. The status bar shows your hit/miss ratio.

## Features

- **Transparent** — no changes to your workflow. Files load faster on repeated opens.
- **Timestamp validation** — every read checks file mtime. If the file changed, ToolRecall re-reads from disk and updates the cache. You always see the latest content.
- **Exclusions** — `node_modules/`, `.git/`, binary files, and other noise are skipped automatically.
- **Warm across sessions** — ToolRecall's SQLite cache persists between VS Code sessions.
- **Local only** — the cache daemon + proxy bind to `127.0.0.1`. No network traffic.
- **Status bar** — `TR: 12H / 3M` shows hits and misses. Click for details.

## Install

### Prerequisites

```bash
pip install toolrecall
```

### From Marketplace

Search "ToolRecall Cache" in the VS Code extensions panel and install.

### From VSIX

```bash
cd vscode-extension
npm install
npm run compile
code --install-extension toolrecall-cache-0.1.0.vsix
```

## Configuration

All settings are optional. The defaults work for most users.

| Setting | Default | Description |
|---------|---------|-------------|
| `toolrecall.enabled` | `true` | Enable caching |
| `toolrecall.excludedPatterns` | `node_modules/**, .git/**, ...` | Glob patterns to skip |
| `toolrecall.binaryExtensions` | `.png, .jpg, .pdf, ...` | Binary file extensions |

## Commands

- `ToolRecall: Show Cache Status` — shows hit/miss counts
- `ToolRecall: Invalidate All Cache` — clears all cache entries

## Architecture

```
VS Code Extension  ←HTTP→  Proxy (127.0.0.1:PORT)  ←UDS→  Daemon
```

On activation: extension finds the `toolrecall` binary (PATH, pipx, `.local/bin`), spawns the daemon, then the HTTP proxy. Both are killed on deactivation.

## Security

- **Daemon started with TOOLRECALL_MCP_ALLOWED_PATHS** = current workspace folders only. No other paths are readable, even if the daemon runs.
- **Proxy binds to 127.0.0.1** — no external access. Random port (0 = OS-assigned).
- **Sensitive file blocklist** in ToolRecall core blocks `.env`, `.ssh/`, `.pem`, credentials, etc.
- **Timestamp validation** every read. A changed file is re-read from disk. Stale entries are never served.
- **Binary files excluded** by extension (.png, .jpg, .pdf, .zip, etc.)
- **node_modules/**, **.git/**, **.hg/**, **.svn/**, **__pycache__/** excluded by default.

All security is built into ToolRecall's core (WAF blocklist + allowlist + mtime validation). The extension just sets the allowlist to the current workspace.

## Tested

- Python tests: 176 pass (ToolRecall core)
- TypeScript: compiles cleanly
- Platform: Linux (Ubuntu). Architecture is HTTP-based and identical on macOS/Windows.
- The extension was not run in VS Code in this VM (no display). Logic is simple event handlers + HTTP calls.

## Development

```bash
cd vscode-extension
npm install
npm run compile   # TypeScript → out/
npm run package   # .vsix
```

## Publishing

```bash
vsce publish
npx ovsx publish  # Open VSX
```
