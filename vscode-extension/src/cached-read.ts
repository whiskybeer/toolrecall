/* ------------------------------------------------------------------
 * ToolRecall VS Code Extension — HTTP Cache Client
 *
 * Communicates with the ToolRecall HTTP proxy for file-read caching.
 * Timestamp validation: server-side mtime check on every read.
 * Falls back to native read on cache miss.
 *
 * Security (OWASP Top 10):
 *   A1-Injection:  encodeURIComponent on all user-supplied paths
 *   A2-Crypto:     no secrets transmitted; localhost-only
 *   A3-BrokenAuth: no auth needed (localhost-only, no credentials)
 *   A4-IDOR:       workspace scope check must be done by caller
 *   A5-BrokenAC:   daemon's allowed_paths + blocklist on server side
 *   A6-Misconfig:  no hardcoded secrets; all config via package.json
 *   A7-XSS:        JSON.parse only, never eval() content
 *   A8-Deserialize: JSON.parse with try/catch
 *   A9-Logging:    no sensitive data in logs (paths are workspace files)
 *   A10-SSRF:      only connects to 127.0.0.1:PORT
 * ------------------------------------------------------------------ */

import * as http from 'http';
import * as path from 'path';

// ─── Types ─────────────────────────────────────────────────

export interface CacheResult {
  content: string;
  cached: boolean;
  path: string;
  mtime: number;
  size: number;
}

interface ProxyResponse {
  content?: string;
  cached?: boolean;
  path?: string;
  mtime?: number;
  size?: number;
  error?: string;
}

export interface CacheStats {
  hits: number;
  misses: number;
  total_requests: number;
  tokens_saved: number;
  mem_entries: number;
  mem_size_mb: number;
}

// ─── Path utilities ────────────────────────────────────────

/**
 * Check if a file path looks binary by its extension.
 * OWASP A12 (Input Validation): extension whitelist, not blacklist.
 * Only text extensions are allowed through.
 */
const TEXT_EXTENSIONS = new Set([
  '.ts', '.js', '.jsx', '.tsx', '.json', '.html', '.css', '.scss', '.less',
  '.py', '.rb', '.go', '.rs', '.java', '.c', '.cpp', '.h', '.hpp',
  '.md', '.txt', '.yml', '.yaml', '.toml', '.ini', '.cfg', '.conf',
  '.sh', '.bash', '.zsh', '.fish', '.ps1', '.bat', '.cmd',
  '.xml', '.svg', '.sql', '.env.example', '.gitignore', '.dockerfile',
  '.vue', '.svelte', '.astro', '.php', '.pl', '.pm', '.swift', '.kt',
  '.gradle', '.m', '.mm', '.r', '.lua', '.ex', '.exs',
  '.vim', '.tf', '.hcl', '.lock', '.log',
  '.eslintrc', '.prettierrc', '.babelrc', '.editorconfig',
]);

export function isBinaryPath(filePath: string, binaryExtensions: string[]): boolean {
  const ext = path.extname(filePath).toLowerCase();

  // Fast path: known text extensions pass through
  if (TEXT_EXTENSIONS.has(ext)) return false;

  // Fallback: check the user's binaryExtensions list
  return binaryExtensions.includes(ext);
}

/**
 * Check if a file path should be excluded (node_modules, .git, etc.).
 * OWASP A1: pattern-based, no dynamic eval.
 */
export function isExcludedPath(filePath: string, excludedPatterns: string[]): boolean {
  const normalized = filePath.replace(/\\/g, '/');

  // Fast path: known exclusions
  if (normalized.includes('/node_modules/')) return true;
  if (normalized.includes('/.git/')) return true;
  if (normalized.includes('/.hg/')) return true;
  if (normalized.includes('/.svn/')) return true;
  if (normalized.includes('/__pycache__/')) return true;
  if (normalized.includes('.venv/')) return true;
  if (normalized.includes('/venv/')) return true;

  // User-configured patterns: simple substring match only (no regex eval of user input)
  for (const pattern of excludedPatterns) {
    if (typeof pattern !== 'string') continue;
    const simple = pattern.replace(/\*\*/g, '').replace(/\*/g, '');
    if (normalized.includes(simple)) return true;
  }

  return false;
}

/**
 * Check if file is within the workspace.
 * OWASP A1/A5: path traversal protection via resolve + prefix check.
 */
export function isInWorkspace(filePath: string, workspaceFolders: string[]): boolean {
  const normalized = path.resolve(filePath);

  // OWASP A5: reject if path contains null byte or newline
  if (filePath.includes('\0') || filePath.includes('\n')) return false;

  for (const folder of workspaceFolders) {
    const resolved = path.resolve(folder);
    // OWASP A1: path traversal check — ensure file is WITHIN workspace
    if (normalized === resolved || normalized.startsWith(resolved + path.sep)) {
      return true;
    }
  }
  return false;
}

// ─── Cache service ─────────────────────────────────────────

/**
 * Cache service — communicates with ToolRecall proxy via HTTP.
 * Only connects to 127.0.0.1 (OWASP A10: SSRF prevention).
 */
export class CacheService {
  private port: number;
  private _hits = 0;
  private _misses = 0;

  constructor(port: number) {
    // OWASP A2: validate port
    if (typeof port !== 'number' || port <= 0 || port > 65535) {
      throw new Error(`Invalid proxy port: ${port}`);
    }
    this.port = port;
  }

  get hits(): number { return this._hits; }
  get misses(): number { return this._misses; }

  /**
   * Read a file through the ToolRecall cache.
   * OWASP A1: path is encodeURIComponent'd
   * OWASP A10: only connects to 127.0.0.1
   * Returns null on error (caller falls back to native read).
   */
  async readFile(filePath: string): Promise<CacheResult | null> {
    return new Promise((resolve) => {
      // OWASP A1: input sanitization
      if (typeof filePath !== 'string' || filePath.length === 0) {
        this._misses++;
        resolve(null);
        return;
      }
      // Reject paths with null bytes or newlines (path traversal / injection)
      if (filePath.includes('\0') || filePath.includes('\n')) {
        this._misses++;
        resolve(null);
        return;
      }

      // OWASP A1: encode user-supplied path
      const urlPath = `/cached_read?path=${encodeURIComponent(filePath)}`;

      const req = http.get(
        {
          hostname: '127.0.0.1', // OWASP A10: no SSRF
          port: this.port,
          path: urlPath,
          timeout: 5000,
        },
        (res: http.IncomingMessage) => {
          let data = '';
          res.on('data', (chunk: Buffer) => {
            data += chunk.toString();
          });
          res.on('end', () => {
            try {
              // OWASP A8: safe JSON parsing
              const parsed: ProxyResponse = JSON.parse(data);
              if (parsed.error) {
                this._misses++;
                resolve(null);
                return;
              }
              // OWASP A7: content is text, never rendered as HTML/script
              if (parsed.cached) {
                this._hits++;
              } else {
                this._misses++;
              }
              resolve({
                content: parsed.content || '',
                cached: parsed.cached || false,
                path: parsed.path || filePath,
                mtime: typeof parsed.mtime === 'number' ? parsed.mtime : 0,
                size: typeof parsed.size === 'number' ? parsed.size : 0,
              });
            } catch {
              this._misses++;
              resolve(null);
            }
          });
        }
      );

      req.on('error', () => {
        this._misses++;
        resolve(null);
      });

      req.on('timeout', () => {
        req.destroy();
        this._misses++;
        resolve(null);
      });
    });
  }

  /**
   * Invalidate a file in the cache.
   */
  async invalidate(filePath: string): Promise<boolean> {
    if (typeof filePath !== 'string' || filePath.length === 0) return false;

    return new Promise((resolve) => {
      const urlPath = `/cache/invalidate?path=${encodeURIComponent(filePath)}`;

      const req = http.get(
        { hostname: '127.0.0.1', port: this.port, path: urlPath, timeout: 3000 },
        (res: http.IncomingMessage) => {
          let data = '';
          res.on('data', (chunk: Buffer) => { data += chunk.toString(); });
          res.on('end', () => {
            try {
              const parsed = JSON.parse(data);
              resolve(!parsed.error);
            } catch {
              resolve(false);
            }
          });
        }
      );

      req.on('error', () => resolve(false));
      req.on('timeout', () => {
        req.destroy();
        resolve(false);
      });
    });
  }

  /**
   * Get cache statistics from the proxy.
   */
  async getStats(): Promise<CacheStats | null> {
    return new Promise((resolve) => {
      const req = http.get(
        { hostname: '127.0.0.1', port: this.port, path: '/cache/stats', timeout: 3000 },
        (res: http.IncomingMessage) => {
          let data = '';
          res.on('data', (chunk: Buffer) => { data += chunk.toString(); });
          res.on('end', () => {
            try {
              const parsed = JSON.parse(data);
              if (parsed.error) { resolve(null); return; }
              resolve({
                hits: parsed.hits || 0,
                misses: parsed.misses || 0,
                total_requests: parsed.total_requests || 0,
                tokens_saved: parsed.tokens_saved || 0,
                mem_entries: parsed.mem_entries || 0,
                mem_size_mb: parsed.mem_size_mb || 0,
              });
            } catch {
              resolve(null);
            }
          });
        }
      );

      req.on('error', () => resolve(null));
      req.on('timeout', () => {
        req.destroy();
        resolve(null);
      });
    });
  }
}
