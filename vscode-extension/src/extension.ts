/* ------------------------------------------------------------------
 * ToolRecall VS Code Extension — Main Entry Point
 *
 * Activates on VS Code startup:
 * 1. Finds toolrecall binary (pip, pipx, PATH)
 * 2. Starts daemon + HTTP proxy as child processes
 * 3. Intercepts document opens for cached reads
 * 4. Shows cache stats in status bar
 *
 * Cross-platform: Windows, macOS, Linux
 * Zero config: just pip install toolrecall + install extension
 * ------------------------------------------------------------------ */

import * as vscode from 'vscode';
import * as http from 'http';
import * as os from 'os';
import { startProxyProcesses, ProxyInfo } from './proxy';
import { CacheService, isBinaryPath, isExcludedPath, isInWorkspace } from './cached-read';
import { StatusBarManager } from './status';

// ─── State ─────────────────────────────────────────────────

let _cacheService: CacheService | null = null;
let _statusBar: StatusBarManager | null = null;
let _proxyInfo: ProxyInfo | null = null;
let _disposables: vscode.Disposable[] = [];

// ─── Activation ────────────────────────────────────────────

export async function activate(context: vscode.ExtensionContext) {
  console.log('[ToolRecall] Activating...');

  // Check if enabled
  const config = vscode.workspace.getConfiguration('toolrecall');
  if (!config.get<boolean>('enabled', true)) {
    console.log('[ToolRecall] Disabled by config');
    return;
  }

  // Create status bar
  _statusBar = new StatusBarManager();
  context.subscriptions.push(_statusBar);

  // Try to start proxy
  try {
    // Collect workspace paths for the daemon's allowlist
    const workspacePaths = (vscode.workspace.workspaceFolders || [])
      .map(f => f.uri.fsPath)
      .join(',');
    const allowedPaths = workspacePaths || os.homedir();

    _proxyInfo = await startProxyProcesses(allowedPaths);
    _cacheService = new CacheService(_proxyInfo.port);
    _statusBar.connected = true;
    console.log(`[ToolRecall] Proxy running on port ${_proxyInfo.port} (PID: ${_proxyInfo.daemonPid})`);
  } catch (err: any) {
    console.warn(`[ToolRecall] Failed to start proxy: ${err.message}`);
    console.log('[ToolRecall] Running in fallback mode (no caching)');
    _statusBar.connected = false;
  }

  // Register event handlers
  registerHandlers(context);

  // Register commands
  registerCommands(context);

  console.log('[ToolRecall] Activated');
}

// ─── Deactivation ──────────────────────────────────────────

export async function deactivate() {
  console.log('[ToolRecall] Deactivating...');

  // Clean up disposables
  for (const d of _disposables) {
    d.dispose();
  }
  _disposables = [];

  // Clean up status bar
  if (_statusBar) {
    _statusBar.dispose();
    _statusBar = null;
  }

  _cacheService = null;
  _proxyInfo = null;
}

// ─── Event Handlers ────────────────────────────────────────

function registerHandlers(context: vscode.ExtensionContext) {
  // Handle document opens — try cached read
  const openHandler = vscode.workspace.onDidOpenTextDocument(async (doc) => {
    await handleDocumentOpen(doc);
  });
  _disposables.push(openHandler);
  context.subscriptions.push(openHandler);

  // Handle document changes — invalidate cache
  const changeHandler = vscode.workspace.onDidChangeTextDocument(async (event) => {
    if (_cacheService && event.document.uri.scheme === 'file') {
      try {
        await _cacheService.invalidate(event.document.fileName);
      } catch {
        // Silently ignore — cache will be invalidated by mtime anyway
      }
    }
  });
  _disposables.push(changeHandler);
  context.subscriptions.push(changeHandler);

  // Handle workspace folder changes — reset stats
  const workspaceHandler = vscode.workspace.onDidChangeWorkspaceFolders(() => {
    if (_statusBar) {
      _statusBar.reset();
    }
  });
  _disposables.push(workspaceHandler);
  context.subscriptions.push(workspaceHandler);

  // Process already-open documents (extension loaded after some docs)
  for (const editor of vscode.window.visibleTextEditors) {
    handleDocumentOpen(editor.document);
  }
}

// ─── Document Open Handler ─────────────────────────────────

async function handleDocumentOpen(doc: vscode.TextDocument) {
  if (!_cacheService || !_statusBar) {
    return;
  }

  // Only interested in file:// URIs
  if (doc.uri.scheme !== 'file') {
    return;
  }

  const filePath = doc.fileName;
  const config = vscode.workspace.getConfiguration('toolrecall');

  // Check workspace
  const workspaceFolders = vscode.workspace.workspaceFolders?.map(f => f.uri.fsPath) || [];
  if (workspaceFolders.length > 0 && !isInWorkspace(filePath, workspaceFolders)) {
    return;
  }

  // Check excluded patterns
  const excludedPatterns = config.get<string[]>('excludedPatterns', []);
  if (isExcludedPath(filePath, excludedPatterns)) {
    return;
  }

  // Check binary extensions
  const binaryExtensions = config.get<string[]>('binaryExtensions', []);
  if (isBinaryPath(filePath, binaryExtensions)) {
    return;
  }

  // Read through cache
  try {
    const result = await _cacheService.readFile(filePath);
    if (result) {
      if (result.cached) {
        _statusBar.recordHit();
      } else {
        _statusBar.recordMiss();
      }
    } else {
      _statusBar.recordMiss();
    }
  } catch {
    _statusBar.recordMiss();
  }
}

// ─── Commands ──────────────────────────────────────────────

function registerCommands(context: vscode.ExtensionContext) {
  // Show status details
  const showStatusCmd = vscode.commands.registerCommand('toolrecall.showStatus', async () => {
    if (_statusBar) {
      await _statusBar.showDetails();
    }
  });
  _disposables.push(showStatusCmd);
  context.subscriptions.push(showStatusCmd);

  // Invalidate all cache
  const invalidateCmd = vscode.commands.registerCommand('toolrecall.invalidateAll', async () => {
    if (_cacheService && _proxyInfo) {
      try {
        await httpGet(`http://127.0.0.1:${_proxyInfo.port}/cache/invalidate`);
        vscode.window.showInformationMessage('ToolRecall cache invalidated');
      } catch {
        vscode.window.showWarningMessage('Failed to invalidate cache — proxy not available');
      }
    }
  });
  _disposables.push(invalidateCmd);
  context.subscriptions.push(invalidateCmd);
}

// ─── Helpers ───────────────────────────────────────────────

/** Simple HTTP GET returning response body (no deps). */
function httpGet(url: string): Promise<string> {
  return new Promise((resolve, reject) => {
    const u = new URL(url);
    const req = http.get(
      { hostname: u.hostname, port: Number(u.port), path: u.pathname, timeout: 5000 },
      (res) => {
        let data = '';
        res.on('data', (chunk) => { data += chunk.toString(); });
        res.on('end', () => resolve(data));
      }
    );
    req.on('error', reject);
    req.on('timeout', () => { req.destroy(); reject(new Error('timeout')); });
  });
}
