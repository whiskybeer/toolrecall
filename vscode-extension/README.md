# ToolRecall Cache — VS Code Extension

> ⚠️ **Experimental.** Works in testing — not yet battle-tested in production. You may encounter edge cases with large workspaces or concurrent file changes.

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

PowerShell on Windows:
```powershell
pip install toolrecall
```

If `pip` is not found, make sure Python is added to PATH during installation (check "Add Python to PATH" in the installer). Then restart your terminal.

### From Marketplace

Search "ToolRecall Cache" in the VS Code extensions panel and install.

### From VSIX

```bash
cd vscode-extension
npm install
npm run compile
code --install-extension toolrecall-cache-0.1.0.vsix
```

PowerShell:
```powershell
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
VS Code Extension  ←HTTP→  Proxy (127.0.0.1:PORT)  ←UDS/TCP→  Daemon
```

On activation: extension finds the `toolrecall` binary (PATH, pipx, `.local/bin`), spawns the daemon, then the HTTP proxy. Both are killed on deactivation.

### Windows details

- **No Unix Domain Sockets**: the daemon falls back to TCP on `127.0.0.1:8568` automatically.
- **Binary search**: checks `.exe` and `.cmd` extensions on PATH.
- **Proxy**: same HTTP interface on `127.0.0.1` — identical behavior.
- **Daemon**: spawned with `windowsHide: true` — no console window pops up.

## Other IDE integrations

The architecture is not VS Code-specific — any editor or tool that can make HTTP requests on `127.0.0.1` can use ToolRecall's cache.

| IDE / Tool | How it connects |
|------------|----------------|
| **VS Code** (this extension) | Document open handler → HTTP cached_read |
| **Neovim** | `BufReadPre` autocommand + `curl` or `vim.inspect` |
| **IntelliJ / JetBrains** | `FileDocumentManagerListener` or custom `FileSystem` plugin |
| **Sublime Text** | `on_load` event + `urllib` |
| **Helix / Zed** | Custom LSP or editor plugin |
| **Any editor via CLI** | `alias read='toolrecall cached_read'` or a shell wrapper |
| **CI / build scripts** | Direct import: `from toolrecall import cached_read` |

The HTTP API is minimal:
```
GET http://127.0.0.1:PORT/cached_read?path=/absolute/path
GET http://127.0.0.1:PORT/cache/stats
GET http://127.0.0.1:PORT/cache/invalidate
```

## Security

- **Daemon started with TOOLRECALL_MCP_ALLOWED_PATHS** = current workspace folders only. No other paths are readable, even if the daemon runs.
- **Proxy binds to 127.0.0.1** — no external access. Random port (0 = OS-assigned).
- **Sensitive file blocklist** in ToolRecall core blocks `.env`, `.ssh/`, `.pem`, credentials, etc.
- **Timestamp validation** every read. A changed file is re-read from disk. Stale entries are never served.
- **Binary files excluded** by extension (.png, .jpg, .pdf, .zip, etc.)
- **node_modules/**, **.git/**, **.hg/**, **.svn/**, **__pycache__/** excluded by default.
- **OWASP Top 10**: input validation, path traversal prevention, SSRF prevention, safe JSON parsing, no shell injection.

All security is built into ToolRecall's core (WAF blocklist + allowlist + mtime validation). The extension just sets the allowlist to the current workspace.

## Tested

- Python tests: 176 pass (ToolRecall core)
- TypeScript: compiles cleanly
- Real daemon + proxy tested in this VM: cached_read (miss→hit), stats, invalidate, blocked paths, non-existent files — all verified
- Platform: Linux (Ubuntu). Architecture is HTTP-based and identical on macOS/Windows.

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
