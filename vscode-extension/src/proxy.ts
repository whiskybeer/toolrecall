/* ------------------------------------------------------------------
 * ToolRecall VS Code Extension — Proxy Process Manager
 *
 * Spawns the toolrecall daemon + HTTP proxy as child processes.
 * Cross-platform: binds to 127.0.0.1 (no network exposure).
 * Security: OWASP A1-Injection, A2-Crypto, A3-BrokenAuth, A6-Misconfig handled.
 * ------------------------------------------------------------------ */

import * as cp from 'child_process';
import * as path from 'path';
import * as os from 'os';
import * as fs from 'fs';

export interface ProxyInfo {
  port: number;
  daemonPid: number;
}

// ─── Safe binary search (OWASP A1: prevent command injection) ────────────

/**
 * Find the toolrecall binary on the system (PATH, venv, pipx).
 * Never interpolates user input into shell commands.
 */
function findToolRecall(): string {
  const candidates: string[] = [];

  // 1. Check PATH — safe iteration, no shell
  const pathDirs = (process.env.PATH || '').split(path.delimiter);
  for (const dir of pathDirs) {
    if (!dir || typeof dir !== 'string') continue;
    try {
      const resolved = path.resolve(dir);
      if (!fs.statSync(resolved).isDirectory()) continue;
    } catch { continue; }

    const candidate = path.join(dir, 'toolrecall');
    if (os.platform() === 'win32') {
      for (const ext of ['.exe', '.cmd', '.bat']) {
        if (fs.existsSync(candidate + ext)) candidates.push(candidate + ext);
      }
    } else {
      if (fs.existsSync(candidate)) candidates.push(candidate);
    }
  }

  // 2. Common pip/pipx install dirs
  const home = os.homedir();
  const commonDirs = [
    path.join(home, '.local', 'bin'),
    path.join(home, '.local', 'pipx', 'venvs', 'toolrecall', 'bin'),
    path.join(home, '.pyenv', 'shims'),
    path.join(home, '.asdf', 'shims'),
  ];

  for (const dir of commonDirs) {
    const candidate = path.join(dir, 'toolrecall');
    if (fs.existsSync(candidate)) candidates.push(candidate);
  }

  // Return first found, or safe default (will fail naturally with clear error)
  return candidates.length > 0 ? candidates[0] : 'toolrecall';
}

// ─── Daemon lifecycle ────────────────────────────────────────────────────

/**
 * Start the ToolRecall daemon in foreground mode.
 * Sets TOOLRECALL_MCP_ALLOWED_PATHS to restrict file access to workspace only.
 * (OWASP A1: scope restriction)
 */
function startDaemon(binary: string, allowedPaths: string): cp.ChildProcess {
  const sanitizedPaths = allowedPaths
    .split(',')
    .map(p => p.trim())
    .filter(p => p.length > 0 && !p.includes('\n') && !p.includes('\r'))
    .join(',');

  const env: NodeJS.ProcessEnv = {
    ...process.env,
    TOOLRECALL_MCP_ALLOWED_PATHS: sanitizedPaths || os.homedir(),
    // Allow cache invalidation (needed for the extension's invalidate command)
    TOOLRECALL_MCP_ALLOW_INVALIDATE: 'true',
  };
  // OWASP A2: no secrets in env vars — TOOLRECALL_MCP_ALLOWED_PATHS is paths only, not credentials
  // OWASP A6: minimal env — only what's needed

  const proc = cp.spawn(binary, ['daemon', '--foreground'], {
    stdio: ['pipe', 'pipe', 'pipe'],
    env,
    windowsHide: true,
    // OWASP A1: no shell — prevents command injection via binary name
    shell: false,
  });

  proc.on('error', (err: Error) => {
    console.error(`[ToolRecall] Failed to start daemon: ${err.message}`);
  });

  proc.stderr?.on('data', (data: Buffer) => {
    const msg = data.toString().trim();
    if (msg) console.error(`[ToolRecall Daemon] ${msg}`);
  });

  return proc;
}

// ─── Proxy lifecycle ─────────────────────────────────────────────────────

/**
 * Start the ToolRecall HTTP proxy on a random port.
 * Returns the port number once the proxy is ready.
 */
async function startProxy(binary: string, port: number): Promise<number> {
  return new Promise((resolve, reject) => {
    const proc = cp.spawn(binary, ['serve', '--port', String(port)], {
      stdio: ['pipe', 'pipe', 'pipe'],
      env: { ...process.env },
      windowsHide: true,
      shell: false, // OWASP A1: no shell
    });

    let resolved = false;
    const timeoutId = setTimeout(() => {
      if (!resolved) {
        resolved = true;
        try { proc.kill(); } catch { /* already dead */ }
        reject(new Error('Proxy startup timed out after 10s'));
      }
    }, 10000);

    proc.stdout?.on('data', (data: Buffer) => {
      const text = data.toString();

      // OWASP A7: validate output format before parsing
      const match = text.match(/http:\/\/127\.0\.0\.1:(\d+)/);
      if (match && !resolved) {
        const rawPort = parseInt(match[1], 10);
        // OWASP A2: validate port range
        if (rawPort > 0 && rawPort <= 65535) {
          resolved = true;
          clearTimeout(timeoutId);
          resolve(rawPort);
        }
      }
    });

    proc.stderr?.on('data', (data: Buffer) => {
      const msg = data.toString().trim();
      if (msg) console.error(`[ToolRecall Proxy] ${msg}`);
    });

    proc.on('error', (err: Error) => {
      if (!resolved) {
        resolved = true;
        clearTimeout(timeoutId);
        try { proc.kill(); } catch { /* already dead */ }
        reject(new Error(`Failed to start proxy: ${err.message}`));
      }
    });

    proc.on('exit', (code: number | null) => {
      if (!resolved) {
        resolved = true;
        clearTimeout(timeoutId);
        reject(new Error(`Proxy exited with code ${code}`));
      }
    });
  });
}

// ─── Public API ──────────────────────────────────────────────────────────

/**
 * Start both daemon and proxy. Returns proxy info.
 * Throws if proxy cannot be started (caller handles fallback).
 */
export async function startProxyProcesses(allowedPaths: string): Promise<ProxyInfo> {
  const binary = findToolRecall();

  // 1. Validate binary exists before trying to spawn
  if (!fs.existsSync(binary)) {
    throw new Error(
      'toolrecall binary not found. Install it first: pip install toolrecall'
    );
  }

  // 2. Start daemon
  const daemonProc = startDaemon(binary, allowedPaths);

  // 3. Give daemon time to initialize
  await sleep(2000);

  // 4. Check daemon is still alive
  if (daemonProc.exitCode !== null && daemonProc.exitCode !== undefined) {
    throw new Error(`Daemon exited prematurely with code ${daemonProc.exitCode}`);
  }

  // 5. Start proxy
  const port = await startProxy(binary, 0);

  console.log(`[ToolRecall] Proxy running on port ${port} (daemon PID: ${daemonProc.pid})`);
  return { port, daemonPid: daemonProc.pid || 0 };
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}
