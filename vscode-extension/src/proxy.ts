/* ------------------------------------------------------------------
 * ToolRecall VS Code Extension — Proxy Process Manager
 *
 * Spawns the toolrecall daemon + HTTP proxy as child processes.
 * Cross-platform: binds to 127.0.0.1 (no network exposure).
 * ------------------------------------------------------------------ */

import * as cp from 'child_process';
import * as path from 'path';
import * as os from 'os';
import * as fs from 'fs';

export interface ProxyInfo {
  port: number;
  daemonPid: number;
}

/**
 * Find the toolrecall binary on the system (PATH, venv, pipx).
 */
function findToolRecall(): string {
  // 1. Check PATH
  const paths = (process.env.PATH || '').split(path.delimiter);
  for (const dir of paths) {
    const candidate = path.join(dir, 'toolrecall');
    if (os.platform() === 'win32') {
      if (fs.existsSync(candidate + '.exe') || fs.existsSync(candidate + '.cmd')) {
        return candidate;
      }
    } else {
      if (fs.existsSync(candidate)) {
        return candidate;
      }
    }
  }

  // 2. Check common pip install locations
  const home = os.homedir();
  const commonDirs: string[] = [
    path.join(home, '.local', 'bin'),
    path.join(home, '.local', 'pipx', 'venvs', 'toolrecall', 'bin'),
    path.join(home, '.pyenv', 'shims'),
    path.join(home, '.asdf', 'shims'),
  ];

  for (const dir of commonDirs) {
    const candidate = path.join(dir, 'toolrecall');
    if (fs.existsSync(candidate)) {
      return candidate;
    }
  }

  // 3. Try `which`/`where` as fallback
  try {
    const which = os.platform() === 'win32' ? 'where' : 'which';
    const result = cp.execSync(`${which} toolrecall 2>/dev/null`, { encoding: 'utf-8', timeout: 5000 });
    const lines = result.trim().split('\n');
    if (lines.length > 0 && lines[0].length > 0) {
      return lines[0];
    }
  } catch {
    // Not found — will fail with clear error
  }

  return 'toolrecall';
}

/**
 * Start the ToolRecall daemon in foreground mode.
 */
function startDaemon(binary: string, allowedPaths: string): cp.ChildProcess {
  const env = {
    ...process.env,
    TOOLRECALL_MCP_ALLOWED_PATHS: allowedPaths,
  };

  const proc = cp.spawn(binary, ['daemon', '--foreground'], {
    stdio: ['pipe', 'pipe', 'pipe'],
    env,
    windowsHide: true,
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
    });

    let resolved = false;
    const stdoutChunks: string[] = [];

    const timeoutId = setTimeout(() => {
      if (!resolved) {
        resolved = true;
        proc.kill();
        reject(new Error('Proxy startup timed out after 10s'));
      }
    }, 10000);

    proc.stdout?.on('data', (data: Buffer) => {
      const text = data.toString();
      stdoutChunks.push(text);

      const match = text.match(/http:\/\/127\.0\.0\.1:(\d+)/);
      if (match && !resolved) {
        resolved = true;
        clearTimeout(timeoutId);
        resolve(parseInt(match[1], 10));
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
        reject(new Error(`Failed to start proxy: ${err.message}`));
      }
    });

    proc.on('exit', (code: number | null) => {
      if (!resolved) {
        resolved = true;
        clearTimeout(timeoutId);
        const output = stdoutChunks.join('');
        reject(new Error(`Proxy exited with code ${code}. Output:\n${output}`));
      }
    });
  });
}

/**
 * Start both daemon and proxy. Returns proxy info.
 */
export async function startProxyProcesses(allowedPaths: string): Promise<ProxyInfo> {
  const binary = findToolRecall();
  console.log(`[ToolRecall] Using binary: ${binary}`);

  // 1. Start daemon in foreground
  const daemonProc = startDaemon(binary, allowedPaths);

  // 2. Give daemon time to initialize
  await sleep(1500);

  // 3. Start proxy on random port
  const port = await startProxy(binary, 0);

  console.log(`[ToolRecall] Proxy running on port ${port} (daemon PID: ${daemonProc.pid})`);
  return { port, daemonPid: daemonProc.pid || 0 };
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}
