/**
 * Content script — runs in page context, extracts page content
 * when asked by the background service worker.
 *
 * Uses defineContentScript() for cross-browser content script registration.
 */

import { defineContentScript } from 'wxt/sandbox';
import browser from 'webextension-polyfill';
import { extractPageContent } from '../page-extractor';

export default defineContentScript({
  matches: ['<all_urls>'],
  main() {
    browser.runtime.onMessage.addListener(
      (msg: unknown, _sender: browser.Runtime.MessageSender) => {
        if ((msg as { type?: string }).type === 'TOOLRECALL_CACHE_NOW') {
          try {
            const extracted = extractPageContent();
            return Promise.resolve({ success: true, data: extracted });
          } catch (err) {
            console.error('[ToolRecall] Extraction error:', err);
            return Promise.resolve({ success: false, error: String(err) });
          }
        }
        return;
      },
    );

    console.log('[ToolRecall] Page extractor content script loaded');
  },
});
