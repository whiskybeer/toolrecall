/** Cache key for browser content */
export interface BrowserCacheKey {
  url: string;
  contentType: 'html' | 'text' | 'snapshot';
  /** Optional: ref ID for element-level caching */
  ref?: string;
}

/** Cache entry stored via ToolRecall */
export interface BrowserCacheEntry {
  url: string;
  contentType: string;
  content: string;
  cachedAt: number;
  hash: string;
  size: number;
}

/** ToolRecall daemon response for cache check */
export interface CacheCheckResponse {
  cached: boolean;
  content?: string;
  error?: string;
  tokens_saved?: number;
  key?: string;
}

/** ToolRecall daemon response for cache store */
export interface CacheStoreResponse {
  stored: boolean;
  error?: string;
}

/** ToolRecall daemon health response */
export interface HealthResponse {
  status: string;
  daemon: string;
  version?: string;
}

/** Cache statistics from daemon */
export interface CacheStats {
  hits: number;
  tokens_saved: number;
  size_bytes?: number;
  entries?: number;
}

/** Messages exchanged between background and content scripts */
export interface CacheNowMessage {
  type: 'TOOLRECALL_CACHE_NOW';
}

export interface CacheNowResponse {
  success: boolean;
  data?: {
    html: string;
    text: string;
    snapshot: string;
    title: string;
    url: string;
    contentHash: string;
  };
}

/** Watched tab state */
export interface WatchedTab {
  tabId: number;
  url: string;
  lastHash: string;
}
