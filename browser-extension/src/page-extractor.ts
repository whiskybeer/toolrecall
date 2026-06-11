/**
 * Page content extractor.
 *
 * Extracts page content in formats that LLM agents typically consume:
 * - HTML: document.documentElement.outerHTML
 * - Text: document.body.innerText (stripped)
 * - Snapshot: simplified interactive-element tree (like browser_snapshot output)
 *
 * All functions are pure — they read from the current DOM snapshot
 * and return structured data. No side effects, no cache calls.
 */

export interface ExtractedContent {
  /** Full page HTML (outerHTML) */
  html: string;
  /** Text content from body.innerText */
  text: string;
  /** Simplified a11y-tree-like snapshot for LLM agents */
  snapshot: string;
  /** document.title */
  title: string;
  /** window.location.href */
  url: string;
  /** Content hash for change detection */
  contentHash: string;
}

// ─── Hash ────────────────────────────────────────────

/**
 * Simple string hash for change detection.
 * Fast, deterministic, no crypto dependencies.
 * Browser extensions can't use crypto.subtle synchronously.
 */
function simpleHash(str: string): string {
  let hash = 0;
  for (let i = 0; i < str.length; i++) {
    const char = str.charCodeAt(i);
    hash = (hash << 5) - hash + char;
    hash |= 0; // Convert to 32-bit integer
  }
  return Math.abs(hash).toString(36);
}

// ─── Snapshot Builder ────────────────────────────────

/**
 * Selectors that define "interactive elements" an LLM agent cares about.
 * Matches browser_navigate's compact view semantics.
 */
const INTERACTIVE_SELECTORS = [
  'a[href]',
  'button',
  'input',
  'textarea',
  'select',
  '[contenteditable=""]',
  '[contenteditable="true"]',
  'summary',
  'details',
  '[role="button"]',
  '[role="link"]',
  '[role="tab"]',
  '[role="menuitem"]',
  '[role="option"]',
  '[role="checkbox"]',
  '[role="radio"]',
  '[role="switch"]',
  '[onclick]',
];

const MAX_TEXT_LENGTH = 100_000; // 100K chars
const MAX_HTML_LENGTH = 500_000; // 500K chars (outerHTML is larger than text)
const MAX_SNAPSHOT_ELEMENTS = 500;
const MAX_ELEMENT_TEXT_LENGTH = 80;

/**
 * Build a compact text snapshot of the page, similar to
 * what browser_navigate returns to LLM agents.
 *
 * Format:
 * ```
 * # Page Title
 * URL: https://...
 *
 * ## Interactive Elements
 * [button] #send "Send Message"
 * [input] .search-box ""
 * [a] → https://example.com
 *
 * ## Text Content
 * Page body text...
 * ```
 */
function buildSnapshot(title: string, url: string): string {
  const elements: string[] = [];

  for (const sel of INTERACTIVE_SELECTORS) {
    try {
      const nodes = document.querySelectorAll(sel);
      for (const el of nodes) {
        if (elements.length >= MAX_SNAPSHOT_ELEMENTS) break;

        const tag = el.tagName.toLowerCase();
        const text = (el.textContent ?? '').trim().substring(0, MAX_ELEMENT_TEXT_LENGTH);
        const id = el.id ? ` #${el.id}` : '';
        const cls = (el as HTMLElement).className;

        let classStr = '';
        if (typeof cls === 'string' && cls) {
          classStr = ` .${cls.split(' ')[0]}`;
        }

        let hrefStr = '';
        if (sel === 'a[href]' || sel === '[role="link"]') {
          hrefStr = ` → ${(el as HTMLAnchorElement).href || ''}`;
        }

        elements.push(`[${tag}]${id}${classStr} "${text}"${hrefStr}`);
      }
    } catch {
      // Cross-origin iframes or shadow DOM boundaries
      continue;
    }
  }

  // Get body text
  const bodyText = (document.body?.innerText ?? '').substring(0, MAX_TEXT_LENGTH);
  const sourceText = bodyText || (document.documentElement?.textContent ?? '').substring(0, MAX_TEXT_LENGTH);

  return [
    `# ${title}`,
    `URL: ${url}`,
    '',
    elements.length > 0
      ? `## Interactive Elements (${elements.length})`
      : '## Interactive Elements (none found)',
    ...elements,
    '',
    '## Text Content',
    sourceText.substring(0, 50_000),
  ].join('\n');
}

// ─── Main Extractor ─────────────────────────────────

/**
 * Extract page content from the current DOM.
 *
 * Returns structured data suitable for caching:
 * - html: full outerHTML
 * - text: innerText (stripped)
 * - snapshot: compact LLM-friendly representation
 * - contentHash: deterministic hash for change detection
 */
export function extractPageContent(): ExtractedContent {
  const url = window.location.href;
  const title = document.title || '(no title)';

  // Full HTML
  const html = (document.documentElement?.outerHTML ?? '').substring(0, MAX_HTML_LENGTH);

  // Body text (stripped of markup)
  const bodyText = document.body?.innerText ?? '';
  const text = bodyText.substring(0, MAX_TEXT_LENGTH);

  // Compact snapshot
  const snapshot = buildSnapshot(title, url);

  // Deterministic hash for change detection
  const contentHash = simpleHash(html + text);

  return { html, text, snapshot, title, url, contentHash };
}
