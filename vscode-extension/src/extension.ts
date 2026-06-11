/* ------------------------------------------------------------------
 * ToolRecall VS Code Extension — Main Entry Point
 *
 * Activates on VS Code startup:
 * 1. Checks if toolrecall is installed — notifies user if missing
 * 2. Starts daemon + HTTP proxy as child processes
 * 3. Intercepts document opens for cached reads
 * 4. Shows cache stats in status bar
 *
 * Cross-platform: Windows, macOS, Linux
 * Zero config: just pip install toolrecall + install extension
 *
 * Security (OWASP Top 10):
 *   A1-Injection:  paths are scoped to workspace, validated, encoded
 *   A2-Crypto:     no secrets, localhost-only traffic
 *   A3-BrokenAuth: not applicable (no auth in local extension)
 *   A4-IDOR:       workspace check prevents reading outside workspace
 *   A5-BrokenAC:   daemon's allowed_paths prevents access to non-workspace
 *   A6-Misconfig:  sensible defaults, all config user-visible
 *   A7-XSS:        content never rendered as HTML/markdown
 *   A8-Deser:      JSON.parse with try/catch
 *   A9-Logging:    no sensitive data logged
 *   A10-SSRF:      127.0.0.1 only
 * ------------------------------------------------------------------ */

import * as vscode from 'vscode';
import * as http from 'http';
import * as os from 'os';
import * as fs from 'fs';
import * as path from 'path';
import { startProxyProcesses, ProxyInfo } from './proxy';
import { CacheService, isBinaryPath, isExcludedPath, isInWorkspace } from './cached-read';
import { StatusBarManager } from './status';

// ─── State ─────────────────────────────────────────────────

let _cacheService: CacheService | null = null;
let _statusBar: StatusBarManager | null = null;
let _proxyInfo: ProxyInfo | null = null;
let _disposables: vscode.Disposable[] = [];
let _activationAttempted = false;

// ─── Activation ────────────────────────────────────────────

export async function activate(context: vscode.ExtensionContext) {
  if (_activationAttempted) return;
  _activationAttempted = true;

  console.log('[ToolRecall] Activating...');

  // Check if enabled
  const config = vscode.workspace.getConfiguration('toolrecall');
  if (!config.get<boolean>('enabled', true)) {
    console.log('[ToolRecall] Disabled by config');
    return;
  }

  // Create status bar immediately
  _statusBar = new StatusBarManager();
  context.subscriptions.push(_statusBar);
  _statusBar.connected = false;

  // Check if toolrecall binary is available
  if (!isToolRecallInstalled()) {
    showInstallPrompt();
    console.log('[ToolRecall] Binary not found — showing install prompt');
    return;
  }

  // Start daemon + proxy
  try {
    const workspacePaths = (vscode.workspace.workspaceFolders || [])
      .map(f => f.uri.fsPath)
      .join(',');

    // OWASP A5: use homedir as fallback — daemon's blocklist prevents access to sensitive files
    const allowedPaths = workspacePaths || os.homedir();

    _proxyInfo = await startProxyProcesses(allowedPaths);
    _cacheService = new CacheService(_proxyInfo.port);
    _statusBar.connected = true;
    console.log(`[ToolRecall] Proxy running on port ${_proxyInfo.port}`);
  } catch (err: any) {
    const msg = err && typeof err.message === 'string' ? err.message : String(err);
    console.error(`[ToolRecall] Proxy startup failed: ${msg}`);
    // StatusBar stays disconnected — graceful fallback
  }

  // Register event handlers (also work in fallback — they silently skip if _cacheService is null)
  registerHandlers(context);
  registerCommands(context);

  console.log('[ToolRecall] Activated (proxy: ' + (_cacheService ? 'running' : 'fallback') + ')');
}

// ─── Deactivation ──────────────────────────────────────────

export async function deactivate() {
  console.log('[ToolRecall] Deactivating...');

  for (const d of _disposables) {
    try { d.dispose(); } catch { /* ignore */ }
  }
  _disposables = [];

  if (_statusBar) {
    try { _statusBar.dispose(); } catch { /* ignore */ }
    _statusBar = null;
  }

  _cacheService = null;
  _proxyInfo = null;
}

// ─── Install check ─────────────────────────────────────────

function isToolRecallInstalled(): boolean {
  // Check PATH for toolrecall
  const pathEnv = process.env.PATH || '';
  const d = path.delimiter;
  const dirs = pathEnv.split(d);
  for (const dir of dirs) {
    try {
      const candidate = dir + path.sep + 'toolrecall';
      if (os.platform() === 'win32') {
        if (fs.existsSync(candidate + '.exe') || fs.existsSync(candidate + '.cmd')) return true;
      } else {
        if (fs.existsSync(candidate)) return true;
      }
    } catch { continue; }
  }

  // Check common locations
  const home = os.homedir();
  const commonLocations = [
    home + '/.local/bin/toolrecall',
    home + '/.local/pipx/venvs/toolrecall/bin/toolrecall',
  ];
  for (const loc of commonLocations) {
    if (fs.existsSync(loc)) return true;
  }

  return false;
}

function showInstallPrompt(): void {
  // OWASP A7: message is controlled text, no user input
  vscode.window.showWarningMessage(
    'ToolRecall binary not found. Install it with: pip install toolrecall',
    'Show Help'
  ).then(selection => {
    if (selection === 'Show Help') {
      vscode.env.openExternal(
        vscode.Uri.parse('https://github.com/whiskybeer/toolrecall#readme')
      );
    }
  });
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
        // Cache will self-invalidate via mtime on next read
      }
    }
  });
  _disposables.push(changeHandler);
  context.subscriptions.push(changeHandler);

  // Handle workspace folder changes — reset stats
  const workspaceHandler = vscode.workspace.onDidChangeWorkspaceFolders(() => {
    if (_statusBar) _statusBar.reset();
  });
  _disposables.push(workspaceHandler);
  context.subscriptions.push(workspaceHandler);

  // Process already-open documents (extension loaded after some docs)
  try {
    for (const editor of vscode.window.visibleTextEditors) {
      handleDocumentOpen(editor.document);
    }
  } catch { /* guard against VS Code API edge cases */ }
}

// ─── Document Open Handler ─────────────────────────────────

async function handleDocumentOpen(doc: vscode.TextDocument) {
  if (!_cacheService || !_statusBar) return;

  // Only interested in file:// URIs (OWASP A10: no SSRF via virtual schemes)
  if (doc.uri.scheme !== 'file') return;

  const filePath = doc.fileName;

  // OWASP A5: check file is within workspace
  const workspaceFolders = vscode.workspace.workspaceFolders?.map(f => f.uri.fsPath) || [];
  if (workspaceFolders.length > 0 && !isInWorkspace(filePath, workspaceFolders)) return;

  const config = vscode.workspace.getConfiguration('toolrecall');

  // OWASP A6: exclusions prevent reading non-code files
  const excludedPatterns = config.get<string[]>('excludedPatterns', []);
  if (isExcludedPath(filePath, excludedPatterns)) return;

  const binaryExtensions = config.get<string[]>('binaryExtensions', []);
  if (isBinaryPath(filePath, binaryExtensions)) return;

  // Read through cache (falls back gracefully on error)
  try {
    const result = await _cacheService.readFile(filePath);
    if (result) {
      if (result.cached) { _statusBar.recordHit(); }
      else { _statusBar.recordMiss(); }
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
    if (_statusBar) await _statusBar.showDetails();
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

/** Simple HTTP GET returning response body (no deps). OWASP A10: only called with controlled 127.0.0.1 URLs. */
function httpGet(url: string): Promise<string> {
  return new Promise((resolve, reject) => {
    try {
      const u = new URL(url);
      // OWASP A10: enforce localhost-only
      if (u.hostname !== '127.0.0.1' && u.hostname !== 'localhost') {
        reject(new Error('SSRF blocked: only localhost allowed'));
        return;
      }
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
    } catch (e) {
      reject(e);
    }
  });
}
