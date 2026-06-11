/**
 * Firefox-specific adaptations for ToolRecall Browser Cache.
 *
 * Firefox uses webRequest where Chrome uses declarativeNetRequest.
 * Firefox also has persistent background pages (no service worker lifecycle).
 *
 * This module provides Firefox-only interceptors that can serve
 * cached responses by redirecting to data: URIs.
 */

import browser from 'webextension-polyfill';
import { ToolRecallClient } from './toolrecall-client';
import type { BrowserCacheKey, CacheCheckResponse } from './types';

/**
 * Detect if we're running in Firefox.
 * Firefox exposes `browser.runtime.getBrowserInfo()` which Chrome doesn't have.
 */
export function isFirefox(): boolean {
  return typeof (browser.runtime as any).getBrowserInfo === 'function';
}

/**
 * Register a Firefox-specific webRequest interceptor.
 *
 * On Firefox, webRequest.onBeforeRequest with 'blocking' can:
 * 1. Check cache for the URL
 * 2. On hit: redirect to a data: URI with cached content
 * 3. On miss: let request through normally
 *
 * This is only registered when running in Firefox.
 *
 * @param client - Initialized ToolRecallClient instance (or null)
 */
export function registerFirefoxInterceptor(
  client: ToolRecallClient | null,
): void {
  if (!isFirefox() || !client) return;

  try {
    browser.webRequest.onBeforeRequest.addListener(
      async (details: browser.WebRequest.OnBeforeRequestDetailsType) => {
        // Only intercept main_frame navigations
        if (details.type !== 'main_frame') return { cancel: false };
        if (!details.url || details.url.startsWith('data:') || details.url.startsWith('about:')) {
          return { cancel: false };
        }

        const key: BrowserCacheKey = { url: details.url, contentType: 'html' };
        const cached: CacheCheckResponse | null = await client.checkCache(key);

        if (cached?.cached && cached.content) {
          console.log(
            `[ToolRecall] Firefox interceptor: serving cached ${details.url}` +
              ` (${cached.content.length} chars, saved ~${cached.tokens_saved} tokens)`,
          );

          // Serve cached HTML via data: URI
          const dataUri = `data:text/html;charset=utf-8,${encodeURIComponent(cached.content)}`;
          return { redirectUrl: dataUri };
        }

        return { cancel: false };
      },
      { urls: ['<all_urls>'], types: ['main_frame'] },
      ['blocking'],
    );

    console.log('[ToolRecall] Firefox webRequest interceptor registered');
  } catch (err) {
    console.warn('[ToolRecall] Failed to register Firefox interceptor:', err);
  }
}

/**
 * Firefox-specific cleanup — remove interceptor listeners.
 */
export function unregisterFirefoxInterceptor(): void {
  if (!isFirefox()) return;
  // Firefox's webRequest cleanup happens when the extension is disabled
}
