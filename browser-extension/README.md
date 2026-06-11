# ToolRecall Browser Cache

> ⚠️ **Experimental.** Works in testing — not yet battle-tested in production. Browser API differences (especially Firefox/Safari) may cause edge cases. The core caching logic is solid; the extension integration layer is new.

**Cache webpage content before sending to LLM agents.**  
Saves tokens, reduces latency, works with any LLM agent using browser tools.

## What It Does

When an LLM agent calls `browser_navigate()` or `browser_snapshot()`, the browser
loads the full page and ships the DOM/HTML/text to the LLM. On every repeat visit,
the same page costs the same tokens and latency.

This extension intercepts page loads:

```
Agent → Browser → Extension checks ToolRecall cache →
  HIT:  cached content returned (0 network, 0 token cost)
  MISS: loads live, caches immediately for next time
```

Token savings: **5K–50K chars per repeated page visit** — 100% of the page content.

## How It Works

1. **Page load detected** — `webNavigation.onCompleted` fires
2. **Content extracted** — HTML, innerText, snapshot (interactive element tree)
3. **Stored in ToolRecall** — via HTTP proxy at `/cached_browser_store`
4. **Repeat visit** — `checkCachedBeforeNavigate` checks ToolRecall via `/cached_browser_check`
5. **Cache hit** — content served without network request

## Prerequisites

- Node.js 18+ for building
- [ToolRecall](https://github.com/whiskybeer/toolrecall) daemon + proxy running

### Windows — One-Click Test

Double-click this from the repo root — it starts daemon + proxy + Chrome automatically:

```powershell
.\browser-extension\scripts\test-on-windows.ps1
```

Requirements: Python 3.11+ with `pip install toolrecall`, and Chrome/Edge/Brave.

The script:
1. Starts `toolrecall daemon` and `toolrecall serve --port 8569`
2. Loads the extension unpacked from `dist/chrome-mv3/`
3. Opens Chrome DevTools automatically
4. Navigates to `example.com` — reload the page to see a CACHE HIT

### Manual Build & Load

```bash
cd browser-extension

# Install dependencies
npm install

# Chromium-based (Chrome, Edge, Brave, Opera)
npm run build:chrome

# Firefox  
npm run build:firefox
```

## Install

### Chrome / Edge / Brave / Opera

1. Go to `chrome://extensions`
2. Enable "Developer mode" (top-right)
3. Click "Load unpacked"
4. Select `browser-extension/dist/chrome-mv3/`

### Firefox

1. Go to `about:debugging#/runtime/this-firefox`
2. Click "Load Temporary Add-on..."
3. Select `browser-extension/dist/firefox-mv3/manifest.json`

### Safari (macOS only)

Safari 16.4+ supports Manifest V3. Build for Safari, then convert:

```bash
# Build the Chrome-compatible output first
npm run build:chrome

# Convert to Safari (requires Xcode CLT on macOS)
xcrun safari-web-extension-converter dist/chrome-mv3/ \
  --app-name "ToolRecall Browser Cache" \
  --bundle-identifier com.toolrecall.browser-cache \
  --project-location ./
```

Then open the generated Xcode project and run the app. Safari Extension Builder
(`Develop → Show Extension Builder`) loads the converted `.appex` bundle.

## Usage

1. Start ToolRecall:
   ```bash
   toolrecall daemon
   toolrecall serve --port 8569
   ```

2. Install the extension (see above).

3. Navigate to any page. Open DevTools → Console → Service Worker / Background page
   to see cache messages:
   ```
   [ToolRecall] Cached https://example.com — title: "Example Domain", hash: a1b2c3
   [ToolRecall] ✅ Cache HIT for https://example.com/page — 12453 chars (saved ~4151 tokens)
   ```

4. Visit the same URL again → cached content is returned, no network request needed.

## Cache Types

| Type | Content | Use case |
|------|---------|----------|
| `html` | Full page HTML (outerHTML) | Full page analysis, rendering |
| `text` | Body innerText (stripped) | Semantic search, summarization |
| `snapshot` | Interactive element tree | Agent navigation (browser_snapshot style) |

## Architecture

```
┌─────────────┐     HTTP (localhost)     ┌──────────────┐     UDS     ┌───────────────┐
│  Browser     │ ◄──────────────────────► │  HTTP Proxy  │ ◄─────────► │  ToolRecall   │
│  Extension   │   /cached_browser_check  │  (proxy.py)  │             │  Daemon       │
│              │   /cached_browser_store  │  port 8569   │             │  (daemon.py)  │
└─────────────┘                          └──────────────┘             └───────┬───────┘
                                                                              │
                                                                     ┌────────▼───────┐
                                                                     │  SQLite Cache  │
                                                                     │  browser_cache │
                                                                     │  table         │
                                                                     └────────────────┘
```

## Supported Browsers

| Browser | API | Status |
|---------|-----|--------|
| Chrome | Manifest V3, `webNavigation` + `scripting` | ✅ |
| Edge | Chrome-compatible (same `.crx` format) | ✅ |
| Brave | Chrome-compatible | ✅ |
| Opera | Chrome-compatible (same `.crx` format) | ✅ |
| Firefox | Manifest V3, `webNavigation` + `webRequest` (blocking) | ✅ |
| Safari | Manifest V3 via `safari-web-extension-converter` | ✅ macOS only |

## Token Savings

A typical page snapshot is 5K–50K chars. At ~3 chars/token:
- **10 repeat visits** to a 15K char page = **~50K tokens saved**
- **50 repeat visits** = **~250K tokens saved**

Results vary by page size and visit frequency.

## Development

```bash
npm run dev          # Watch mode (Chrome/Edge/Brave/Opera)
npm run dev:firefox  # Watch mode (Firefox)
npm run dev:safari   # Watch mode (Safari, macOS only)
npm run lint         # TypeScript type check
npm test             # Run tests (vitest)
npm test:watch       # Test watch mode
```

## Tests

```bash
npm test
```

Tests cover:
- `ToolRecallClient` — port validation, cache check/store, discovery
- `page-extractor` — HTML/text/snapshot extraction, hashing, edge cases

## Security

- Proxy binds to `127.0.0.1` only — no network exposure
- Extension communicates only with localhost proxy
- ToolRecall's SecurityGate filters sensitive content
- No user data exfiltrated — all content stays local

## License

MIT — same as ToolRecall.
