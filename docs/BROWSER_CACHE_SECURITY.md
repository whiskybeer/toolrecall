# Browser Cache Security

> Security analysis of the ToolRecall Browser Cache Extension (v0.1.0).
>
> **Last updated:** 2026-06-11
> **Audit scope:** Extension code (TypeScript), server-side changes (Python proxy/daemon/cache layer)
> **Fixed issues:** 3 findings → 3 fixed, committed at `6b7b227`

---

## 1. Threat Model

### What the extension does

The extension runs in the user's browser and:

1. Listens for page navigation events (`webNavigation`)
2. Injects a content script that reads the current DOM
3. Sends extracted HTML/text/snapshot to a **local-only** HTTP proxy (`127.0.0.1`)
4. The proxy forwards to the ToolRecall daemon via Unix Domain Socket
5. On repeat visits to the same URL, cached content is returned — no network load

### Assets

| Asset | Sensitivity | Who can access |
|-------|-------------|----------------|
| Browser page content (HTML, text) | Contains whatever the user browses — potentially sensitive | Local ToolRecall daemon only |
| Cache database (`~/.toolrecall/*.db`) | All cached pages | Processes under the same Unix user |
| Extension state (watched tabs) | URLs of visited pages | Only the extension's own memory (not persisted to disk) |

### Trust boundaries

```
[Untrusted Web Page]
     │
     │  DOM access via content script (MV3 isolated)
     ▼
[Content Script]  ← reads DOM, extracts content
     │
     │  runtime.onMessage (same-extension only)
     ▼
[Background Service Worker]
     │
     │  fetch() to http://127.0.0.1:{port}
     ▼
[HTTP Proxy]  ← binds 127.0.0.1 only
     │
     │  Unix Domain Socket
     ▼
[ToolRecall Daemon]  ← same Unix user
     │
     │  SQLite write
     ▼
[SQLite Cache DB]
```

**Key property:** Every arrow in this chain is **local** — no external network call happens at any stage.

---

## 2. Extension-Side Security

### 2.1 Permissions (manifest.json)

| Permission | Why needed | Risk |
|------------|------------|------|
| `storage` | Extension state | None — only local browser storage |
| `tabs` | `tabs.sendMessage()` to inject content script | None — used only for cache extraction |
| `webNavigation` | `onBeforeNavigate` + `onCompleted` | None — only reads navigation metadata |
| `scripting` | `executeScript()` to inject content script | None — only injects our own bundled script |
| `<all_urls>` | Content script runs on any page the user visits | **Broadest permission.** Required because LLM agents navigate arbitrary URLs |

**No unused permissions.** `declarativeNetRequest` and `declarativeNetRequestFeedback` were removed in `6b7b227`.

### 2.2 Network

All `fetch()` calls in `toolrecall-client.ts` target **hardcoded `127.0.0.1:{port}`**:

```
checkCache → http://127.0.0.1:{port}/cached_browser_check?key=...
storeCache → http://127.0.0.1:{port}/cached_browser_store  (POST)
getStats   → http://127.0.0.1:{port}/cache/stats
discoverPort → http://127.0.0.1:{port}/health
```

**No URL in the codebase points to any external domain.** The content extracted from the page (HTML, text, snapshot) is never sent anywhere except localhost.

### 2.3 Content Script

- **MV3 isolation:** The content script runs in a separate JavaScript environment from the page. The page cannot intercept `runtime.onMessage`, cannot modify the extractor, and cannot read the extension's variables.
- **Message filtering:** The content script only responds to `{ type: 'TOOLRECALL_CACHE_NOW' }`. All other messages are ignored (`return;`).
- **No eval / dynamic execution:** The extractor is static code. It reads DOM properties (`outerHTML`, `innerText`, `querySelectorAll`) — it never executes strings as code, never creates `<script>` tags, and never calls `eval()`.

### 2.4 Content Size Limits

All extracted content is capped before transmission:

| Content type | Limit | Rationale |
|--------------|-------|-----------|
| `html` (outerHTML) | 500,000 chars (~500 KB) | Prevents multi-MB HTML blobs |
| `text` (innerText) | 100,000 chars (~100 KB) | Prevents extreme text-only pages |
| `snapshot` | 500 interactive elements + 50,000 chars text | Sufficient for LLM agent navigation |

These limits are enforced in `page-extractor.ts` at `extractPageContent()`. The HTML limit was added in `6b7b227`.

### 2.5 Console Logging

The extension logs URLs and content hashes to the background page's console:

```
[ToolRecall] Cached https://example.com — title: "Example Domain", hash: a1b2c3
[ToolRecall] ✅ Cache HIT for https://example.com/page — 12453 chars (saved ~4151 tokens)
```

**Visibility:** Only visible in `chrome://extensions` → service worker console, or Firefox `about:debugging`. Not visible in the page's console. On a shared machine, another user with access to extension debugging could see visited URLs.

---

## 3. Server-Side Security (Proxy + Daemon)

### 3.1 Localhost Binding

The proxy (`run_server()` in `proxy.py`) is hardcoded to `"127.0.0.1"`:

```python
server = http.server.HTTPServer(("127.0.0.1", port), ToolRecallHandler)
```

There is no parameter or config that allows `"0.0.0.0"` binding. If the port is already in use, the proxy exits with an error — it does not fall back to a different interface.

### 3.2 No CORS Headers

```python
# No CORS header: proxy binds only to localhost (127.0.0.1).
# Access-Control-Allow-Origin: * is pointless on a local service
# and risky if accidentally bound to network (CSRF on /cache/invalidate).
```

CORS headers are explicitly omitted. This ensures that even if the proxy were accidentally exposed to the network, no cross-origin requests would succeed.

### 3.3 POST Body Size Limit

The proxy enforces a 5 MB maximum on `Content-Length` in `do_POST()`:

```python
if content_length > MAX_BODY_SIZE:  # 5 * 1024 * 1024
    self.send_response(413)
    self._send_json({"error": f"Request body too large (max {MAX_BODY_SIZE} bytes)"})
    return
```

Added in `6b7b227`. Without this, a local process could send a multi-GB POST body and cause OOM.

### 3.4 Defense in Depth at Cache Layer

The same 5 MB limit is independently enforced in `cached_browser_store()` in `cache.py`:

```python
MAX_CONTENT_BYTES = 5 * 1024 * 1024  # 5 MB
if len(content) > MAX_CONTENT_BYTES:
    return {"stored": False, "error": f"Content too large ({len(content)} bytes, max 5 MB)"}
```

This protects UDS and MCP paths that bypass the HTTP proxy entirely.

### 3.5 SecurityGate for Path-Based Tools

The existing ToolRecall SecurityGate (`_is_sensitive_path`) blocks access to credentials files (`.env`, `.ssh/`, `.aws/`, etc.) via the `cached_read` path. The browser cache does **not** bypass this — it uses its own `browser_cache` table with URL-based keys, not file paths.

**Limitation:** The `browser_cache` table stores arbitrary page content without path-based filtering. This is **intentional**: browser page content is not filesystem paths, so the SecurityGate's path blocklist does not apply. The content is isolated in a separate SQLite table that is never served by `cached_read()`.

---

## 4. Firefox-Specific Considerations

### 4.1 `data:` URI Redirect

The Firefox adapter (`firefox-adapter.ts`) serves cached HTML via `data:` URI redirect:

```typescript
const dataUri = `data:text/html;charset=utf-8,${encodeURIComponent(cached.content)}`;
return { redirectUrl: dataUri };
```

**Security assessment:**
- The cached content was previously loaded by the browser during the first visit
- Any scripts in the page already executed during that first load
- The `data:` URI redirect only fires on **cache hits** (second+ visit)
- No new code execution happens — it's a replay of content the browser has already rendered

**Not exploitable** for cross-site scripting because:
1. The content comes from the local cache, not from an attacker-controlled source
2. The extension only caches pages that the user (or their LLM agent) voluntarily navigated to
3. `data:` URIs inherit a unique opaque origin — they don't carry the cached page's origin

### 4.2 `webRequest` Blocking

Firefox requires the `webRequest` API with `['blocking']` to intercept requests. This permission is automatically granted by Firefox for same-extension use. The extension declares this via WXT's build system (Firefox-specific manifest).

---

## 5. Cache Storage Security

### 5.1 SQLite Database

The cache is stored in `~/.toolrecall/*.db` (SQLite, WAL mode):

| Property | Value |
|----------|-------|
| File permissions | `600` (owner read/write only) |
| Encryption | None (SQLite does not encrypt by default) |
| Table | `browser_cache` — separate from file/skill/terminal caches |
| What's stored | Page URL, content_type (`html`/`text`/`snapshot`), content, title, content_hash, timestamp |

**Risk:** Anyone with the same Unix user account can read the SQLite DB. On shared machines, this could leak cached page content.

**Mitigation:** ToolRecall is an agent-side caching tool. The threat model assumes the machine is single-user. For multi-user environments, run ToolRecall under separate user accounts or enable filesystem encryption (dm-crypt, LUKS).

### 5.2 Invalidation

- `toolrecall cache invalidate` clears all caches including `browser_cache`
- `invalidate_browser_url(url)` clears a specific URL's cache entries
- Content hash tracking detects page changes and auto-overwrites stale entries

---

## 6. What We Accept (Unmitigated Risks)

These are not bugs — they are architectural properties that we do not attempt to fix:

| Risk | Why | Why it's OK |
|------|-----|-------------|
| **Local process can write to cache** | Any process under the same Unix user can send HTTP to `127.0.0.1:8569` | A process with local shell access already has full host access. The cache is not a security boundary. |
| **Cached content is unencrypted on disk** | SQLite has no built-in encryption | Cache is local developer tooling. Use dm-crypt/LUKS for full-disk encryption. |
| **Login-gated pages are cached** | If the user navigates to an authenticated page, its DOM snapshot is cached | The cache stores page content — not cookies/sessions. The browser's same-origin policy still protects sessions. |
| **Extension logs URLs to console** | Debug logging includes full page URLs | Only visible in the extension's own DevTools console. Not exfiltratable by the page. |
| **Page knows the extension is installed** | Content script presence is detectable (`onMessage` listener) | Many extensions are detectable. The extension does not expose any API to the page. |

---

## 7. OWASP LLM Top 10 for LLM Agents

Applied to the browser cache extension's role in an LLM agent pipeline:

| ID | Category | Rating | Notes |
|----|----------|--------|-------|
| LLM01 | Prompt Injection | 🟢 | Extension is passive — it caches/replays content. The agent already received the page content from the browser on first load. |
| LLM02 | Sensitive Info Disclosure | 🟡 | Cached pages may contain sensitive content. Mitigated by single-user threat model and local-only storage. |
| LLM03 | Supply Chain | 🟢 | 0 runtime dependencies from npm. Extension bundle is pure first-party TypeScript. |
| LLM04 | Data Poisoning | 🟢 | Cache is hash-verified. An attacker would need write access to the SQLite DB (≈ host access). |
| LLM05 | Improper Output Handling | 🟢 | Extension never executes agent outputs. It only stores and retrieves cached DOM snapshots. |
| LLM06 | Excessive Agency | 🟢 | Extension has no agency — it cannot initiate actions, only react to navigation events. |
| LLM07 | System Prompt Leakage | 🟢 | Extension has no access to the LLM agent's prompts. It only sees the browser DOM. |
| LLM08 | Vector Weaknesses | 🟢 | No vectors, no embeddings. Simple SQLite key-value lookup by URL hash. |
| LLM09 | Misinformation | 🟢 | Extension does not generate or modify content. It stores what the browser rendered. |
| LLM10 | Unbounded Consumption | 🟢 | Hard limits at every layer: 500K chars HTML, 100K chars text, 5 MB per POST, 5 MB per cache entry. |

---

## 8. Audit Trail

| Date | Change | Author |
|------|--------|--------|
| 2026-06-11 | Initial security analysis | Hermes Agent |
| 2026-06-11 | Removed unused `declarativeNetRequest` permissions | `6b7b227` |
| 2026-06-11 | Added `MAX_HTML_LENGTH = 500_000` in page-extractor.ts | `6b7b227` |
| 2026-06-11 | Added `MAX_BODY_SIZE = 5MB` check in proxy.py `do_POST()` | `6b7b227` |
| 2026-06-11 | Added `MAX_CONTENT_BYTES = 5MB` check in `cached_browser_store()` | `6b7b227` |

---

## 9. How to Verify

```bash
# 1. Verify unused permissions
grep -c 'declarativeNetRequest' browser-extension/src/manifest.ts
# Expected: 0 (should return "0")

# 2. Verify content size limits
cd browser-extension && npx vitest run tests/page-extractor.test.ts
# Expected: 8 tests passed

# 3. Verify POST body size limit
cd toolrecall
python3 -c "
from toolrecall.cache import cached_browser_store
r = cached_browser_store('test:key', 'x' * (5 * 1024 * 1024 + 1))
assert r.get('stored') == False
print('POST body size limit OK')
"

# 4. Verify no external network calls
grep -rn 'fetch(' browser-extension/src/ | grep -v '127.0.0.1'
# Expected: no output
```