/* ------------------------------------------------------------------
 * ToolRecall VS Code Extension — HTTP Cache Client
 *
 * Communicates with the ToolRecall HTTP proxy for file-read caching.
 * Timestamp validation: server-side mtime check on every read.
 * Falls back to native read on cache miss.
 * ------------------------------------------------------------------ */

import * as http from 'http';
import * as path from 'path';

/** Cache result from the proxy */
export interface CacheResult {
  content: string;
  cached: boolean;
  path: string;
  mtime: number;
  size: number;
}

/** Proxy response for cached_read */
interface ProxyResponse {
  content?: string;
  cached?: boolean;
  path?: string;
  mtime?: number;
  size?: number;
  error?: string;
}

/** Aggregated cache statistics */
export interface CacheStats {
  hits: number;
  misses: number;
  total_requests: number;
  tokens_saved: number;
  mem_entries: number;
  mem_size_mb: number;
}

/**
 * Check if a file path looks binary by its extension.
 */
export function isBinaryPath(filePath: string, binaryExtensions: string[]): boolean {
  const ext = path.extname(filePath).toLowerCase();
  return binaryExtensions.includes(ext);
}

/**
 * Check if a file path should be excluded (node_modules, .git, etc.).
 */
export function isExcludedPath(filePath: string, excludedPatterns: string[]): boolean {
  // Normalize to forward slashes for pattern matching
  const normalized = filePath.replace(/\\/g, '/');

  for (const pattern of excludedPatterns) {
    // Simple substring check for common patterns
    const simplePattern = pattern
      .replace(/\*\*/g, '')
      .replace(/\*/g, '');
    if (normalized.includes(simplePattern)) {
      return true;
    }

    // Try regex match for sophisticated patterns
    try {
      const globMatch = pattern
        .replace(/\*\*\/\*\*/g, '**')
        .replace(/\*\*/g, '.*')
        .replace(/\*/g, '[^/]*')
        .replace(/\?/g, '.');
      const regex = new RegExp('^' + globMatch + '$');
      if (regex.test(normalized)) {
        return true;
      }
    } catch {
      // Fall through
    }
  }

  return false;
}

/**
 * Check if file is within the workspace.
 */
export function isInWorkspace(filePath: string, workspaceFolders: string[]): boolean {
  const normalized = path.resolve(filePath);
  for (const folder of workspaceFolders) {
    const resolved = path.resolve(folder);
    if (normalized === resolved || normalized.startsWith(resolved + path.sep)) {
      return true;
    }
  }
  return false;
}

/**
 * Cache service — communicates with ToolRecall proxy via HTTP.
 */
export class CacheService {
  private port: number;
  private _hits = 0;
  private _misses = 0;

  constructor(port: number) {
    this.port = port;
  }

  get hits(): number { return this._hits; }
  get misses(): number { return this._misses; }

  /**
   * Read a file through the ToolRecall cache.
   * Returns content + cache status, or null on error (fallback to native read).
   */
  async readFile(filePath: string): Promise<CacheResult | null> {
    return new Promise((resolve) => {
      const urlPath = `/cached_read?path=${encodeURIComponent(filePath)}`;

      const req = http.get(
        {
          hostname: '127.0.0.1',
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
              const parsed: ProxyResponse = JSON.parse(data);
              if (parsed.error) {
                this._misses++;
                resolve(null);
                return;
              }
              if (parsed.cached) {
                this._hits++;
              } else {
                this._misses++;
              }
              resolve({
                content: parsed.content || '',
                cached: parsed.cached || false,
                path: parsed.path || filePath,
                mtime: parsed.mtime || 0,
                size: parsed.size || 0,
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
    return new Promise((resolve) => {
      const urlPath = `/cache/invalidate?path=${encodeURIComponent(filePath)}`;

      const req = http.get(
        {
          hostname: '127.0.0.1',
          port: this.port,
          path: urlPath,
          timeout: 3000,
        },
        (res: http.IncomingMessage) => {
          let data = '';
          res.on('data', (chunk: Buffer) => {
            data += chunk.toString();
          });
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
        {
          hostname: '127.0.0.1',
          port: this.port,
          path: '/cache/stats',
          timeout: 3000,
        },
        (res: http.IncomingMessage) => {
          let data = '';
          res.on('data', (chunk: Buffer) => {
            data += chunk.toString();
          });
          res.on('end', () => {
            try {
              const parsed = JSON.parse(data);
              if (parsed.error) {
                resolve(null);
                return;
              }
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
