# ToolRecall VS Code Extension — Implementation Plan

## Goal
Transparent file-read caching for VS Code via ToolRecall's HTTP proxy, with timestamp validation on every read. Zero config. Local only.

## Architecture

```
VS Code Extension (Node.js)
  ├── starts ToolRecall HTTP proxy as child process on activate
  │   └── binds to 127.0.0.1:random-port
  ├── onDidOpenTextDocument → cached_read via HTTP
  │   ├── mtime validation server-side
  │   ├── skip node_modules/ .git/ binary
  │   └── fallback: native file read on cache miss
  ├── onDidChangeTextDocument → put invalidate
  └── StatusBar: hit/miss counter
```

## Components

### 1. HTTP Proxy Client (Launcher)
- `findToolRecall()` — locate `toolrecall` binary (pip-installed, PATH, venv, pipx)
- `startProxy()` — spawn `toolrecall proxy --port 0` (random port), read port from stdout
- `stopProxy()` — kill on deactivate
- Store proxy URL, track running state

### 2. File Cache Service
- `readFile(path)` → HTTP POST to `/api/cached_read` → returns content + hit/miss status
- Server does mtime validation internally
- `invalidate(path)` → POST `/api/cache/invalidate`
- `getStats()` → GET `/api/cache/stats`

### 3. Document Open Handler
- `onDidOpenTextDocument`:
  - Check: is in workspace? not node_modules/.git? not binary?
  - Call `cachedRead()`
  - On hit: transparent (content is already loaded by VS Code)
  - On miss: normal read (VS Code default)
  - Update StatusBar

- `onDidChangeTextDocument`:
  - Call `invalidate()` for changed file

### 4. StatusBar
- Show: `TR: 12H / 3M` (hits / misses)
- Reset on workspace change

### 5. Activation
- `activate()`:
  - Find toolrecall binary
  - Start proxy
  - Register event listeners
  - Create StatusBar item
  - Log startup info

- `deactivate()`:
  - Kill proxy process
  - Clean up

## Files
```
vscode-extension/
├── package.json
├── tsconfig.json
├── .vscodeignore
├── src/
│   ├── extension.ts        # activate / deactivate
│   ├── proxy.ts            # spawn/manage toolrecall proxy
│   ├── cached-read.ts      # HTTP cached_read client
│   └── status.ts           # StatusBar component
└── test/
    └── extension.test.ts
```

## Publishing
- `vsce package` → `.vsix`
- Marketplace + Open VSX via `vsce publish`
