/**
 * Background service worker for ToolRecall Browser Cache.
 *
 * Uses WXT's defineBackground() which handles cross-browser abstraction:
 * - Chrome MV3 → service worker
 * - Firefox MV3 → persistent background page
 *
 * Responsibilities:
 *   - Connect to ToolRecall daemon via HTTP proxy
 *   - Listen for webNavigation events (page loads)
 *   - Inject content script, extract page content, cache via ToolRecall
 *   - Serve cached content on repeat page loads
 *   - Track watched tabs for hash-based change detection
 */

import { defineBackground } from 'wxt/sandbox';
import browser from 'webextension-polyfill';
import { ToolRecallClient } from '../toolrecall-client';
import { updateDnrRules, removeDnrRules } from '../dnr-rules';
import type { BrowserCacheKey, WatchedTab } from '../types';

export default defineBackground(() => {
  let client: ToolRecallClient | null = null;
  let proxyPort: number | null = null;

  // ─── Watched tab state ─────────────────────────────────

  const watchedTabs = new Map<number, WatchedTab>();

  function watchTab(tabId: number, url: string): void {
    watchedTabs.set(tabId, { tabId, url, lastHash: '' });
  }

  function unwatchTab(tabId: number): void {
    watchedTabs.delete(tabId);
  }

  // ─── Init ──────────────────────────────────────────────

  async function init(): Promise<void> {
    console.log('[ToolRecall] Initialising browser cache extension...');

    proxyPort = await ToolRecallClient.discoverPort();
    if (proxyPort) {
      client = new ToolRecallClient(proxyPort);
      console.log(`[ToolRecall] Connected to proxy on port ${proxyPort}`);
    } else {
      console.log('[ToolRecall] ToolRecall daemon not found — running without cache backend');
    }

    // Install DNR rules for API call interception (forward proxy)
    // The forward proxy runs on port 8080 by default
    await updateDnrRules(8080);
  }

  // ─── Cache page content after load ───────────────────

  async function cachePageContent(tabId: number, url: string): Promise<void> {
    if (!client || !proxyPort) return;

    try {
      // Inject content script if needed
      try {
        await browser.scripting.executeScript({
          target: { tabId },
          files: ['content-scripts/page-extractor.js'],
        });
      } catch {
        // Already injected
      }

      const response = await browser.tabs.sendMessage(tabId, {
        type: 'TOOLRECALL_CACHE_NOW',
      });

      if (!response?.success || !response?.data) return;

      const { html, text, snapshot, title, contentHash } = response.data;

      if (!html && !text) return;

      // Cache all three formats
      const types: Array<'html' | 'text' | 'snapshot'> = ['html', 'text', 'snapshot'];
      for (const contentType of types) {
        const content =
          contentType === 'html' ? html
          : contentType === 'text' ? text
          : snapshot;
        if (!content) continue;
        await client.storeCache({ url, contentType }, content);
      }

      watchedTabs.set(tabId, { tabId, url, lastHash: contentHash });
      console.log(`[ToolRecall] Cached ${url} — title: "${title}", hash: ${contentHash}`);
    } catch (err) {
      console.debug(`[ToolRecall] Cache error for ${url}:`, err);
    }
  }

  // ─── Check cached content before navigation ───────────

  async function checkCachedBeforeNavigate(tabId: number, url: string): Promise<void> {
    if (!client) return;

    watchTab(tabId, url);

    const key: BrowserCacheKey = { url, contentType: 'snapshot' };
    const result = await client.checkCache(key);

    if (result?.cached && result.content) {
      console.log(
        `[ToolRecall] ✅ Cache HIT for ${url} — ${result.content.length} chars` +
          (result.tokens_saved ? ` (saved ~${result.tokens_saved} tokens)` : ''),
      );
    }
  }

  // ─── Event listeners ─────────────────────────────────

  browser.webNavigation.onBeforeNavigate.addListener(
    (details: browser.WebNavigation.OnBeforeNavigateDetailsType) => {
      if (details.frameId !== 0) return;
      if (!details.url || details.url === 'about:blank') return;
      checkCachedBeforeNavigate(details.tabId, details.url);
    },
  );

  browser.webNavigation.onCompleted.addListener(
    (details: browser.WebNavigation.OnCompletedDetailsType) => {
      if (details.frameId !== 0) return;
      if (!details.url || details.url === 'about:blank') return;
      setTimeout(() => cachePageContent(details.tabId, details.url), 500);
    },
  );

  browser.tabs.onRemoved.addListener((tabId: number) => {
    unwatchTab(tabId);
  });

  browser.action.onClicked.addListener(async () => {
    const stats = await client?.getStats();
    const msg = stats
      ? `ToolRecall Cache\nHits: ${stats.hits}\nTokens saved: ${stats.tokens_saved}`
      : 'ToolRecall Browser Cache\n(daemon not connected)';
    console.log(msg);
  });

  // ─── Init ─────────────────────────────────────────────

  init().catch(console.warn);
});
