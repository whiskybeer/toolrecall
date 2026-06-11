/**
 * ToolRecall HTTP Client for browser extension.
 *
 * Communicates with the ToolRecall daemon via its HTTP proxy bridge.
 * The proxy runs on 127.0.0.1:PORT and forwards requests to the daemon's UDS.
 *
 * All methods are defensive: any failure (daemon not running, timeout, bad response)
 * returns null/false so the extension degrades gracefully.
 */

import type {
  BrowserCacheKey,
  CacheCheckResponse,
  CacheStats,
  HealthResponse,
} from './types';

/** Default ToolRecall proxy ports to try during discovery */
const DEFAULT_PORTS = [8569, 8570, 8571, 8572];

/** Timeout for health checks during port discovery (ms) */
const DISCOVER_TIMEOUT_MS = 500;

/** Timeout for cache operations (ms) */
const CACHE_TIMEOUT_MS = 3000;

export class ToolRecallClient {
  private readonly host: string;

  /**
   * @param port - ToolRecall HTTP proxy port (default: 8569)
   */
  constructor(port: number) {
    if (port <= 0 || port > 65535) {
      throw new Error(`Invalid port: ${port}`);
    }
    this.host = `127.0.0.1:${port}`;
  }

  // ─── Cache Operations ─────────────────────────────────

  /**
   * Check if page content for a URL is already cached.
   * Returns cache hit data or null on miss/error.
   */
  async checkCache(key: BrowserCacheKey): Promise<CacheCheckResponse | null> {
    try {
      const cacheKey = this.buildCacheKey(key);
      const url = `http://${this.host}/cached_browser_check?key=${encodeURIComponent(cacheKey)}`;
      const res = await fetch(url, { signal: AbortSignal.timeout(CACHE_TIMEOUT_MS) });
      if (!res.ok) return null;
      return (await res.json()) as CacheCheckResponse;
    } catch {
      return null; // daemon not running — silently fall through
    }
  }

  /**
   * Store page content in ToolRecall cache.
   * Returns true on success, false otherwise.
   */
  async storeCache(key: BrowserCacheKey, content: string): Promise<boolean> {
    try {
      const cacheKey = this.buildCacheKey(key);
      const url = `http://${this.host}/cached_browser_store`;
      const res = await fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          key: cacheKey,
          content,
          url: key.url,
          contentType: key.contentType,
        }),
        signal: AbortSignal.timeout(CACHE_TIMEOUT_MS),
      });
      return res.ok;
    } catch {
      return false;
    }
  }

  // ─── Stats ────────────────────────────────────────────

  /**
   * Get cache statistics from the daemon.
   * Returns stats object or null if unavailable.
   */
  async getStats(): Promise<CacheStats | null> {
    try {
      const url = `http://${this.host}/cache/stats`;
      const res = await fetch(url, { signal: AbortSignal.timeout(CACHE_TIMEOUT_MS) });
      if (!res.ok) return null;
      return (await res.json()) as CacheStats;
    } catch {
      return null;
    }
  }

  // ─── Discovery ──────────────────────────────────────────

  /**
   * Discover the ToolRecall HTTP proxy port by probing common ports.
   * Returns the first responding port, or null if no proxy is reachable.
   */
  static async discoverPort(): Promise<number | null> {
    for (const port of DEFAULT_PORTS) {
      try {
        const res = await fetch(`http://127.0.0.1:${port}/health`, {
          signal: AbortSignal.timeout(DISCOVER_TIMEOUT_MS),
        });
        if (res.ok) {
          const body = (await res.json()) as HealthResponse;
          if (body.status === 'ok' || body.daemon === 'connected') {
            return port;
          }
        }
      } catch {
        continue;
      }
    }
    return null;
  }

  // ─── Helpers ─────────────────────────────────────────

  /**
   * Build a cache key string for a browser page.
   * Format: `browser:page:<encoded_url>:<content_type>`
   */
  private buildCacheKey(key: BrowserCacheKey): string {
    const ref = key.ref ? `:${key.ref}` : '';
    return `browser:page:${this.encodeUrl(key.url)}:${key.contentType}${ref}`;
  }

  /**
   * Encode a URL into a safe key component.
   * Replaces non-alphanumeric chars with _, max 200 chars.
   */
  private encodeUrl(url: string): string {
    return url.replace(/[^a-zA-Z0-9]/g, '_').substring(0, 200);
  }
}
