# Browser Cache Integration

> How the ToolRecall Browser Cache Extension integrates with LLM agents
> that use browser tools (browser_navigate, browser_snapshot).

## Data Flow

```
LLM Agent (Hermes TUI, Claude Code, etc.)
  │
  │  browser_navigate('https://example.com')
  │
  ▼
Browser Extension
  │
  ├── onBeforeNavigate → checkCachedBeforeNavigate()
  │     │
  │     ├─ Cache HIT: log to console, page loads from cache
  │     │              (snapshot returned from ToolRecall)
  │     │
  │     └─ Cache MISS: nothing special, page loads normally
  │
  └── onCompleted → cachePageContent()
        │
        ├─ 1. Inject content script (if needed)
        ├─ 2. Extract: HTML, innerText, snapshot
        ├─ 3. Store all 3 formats in ToolRecall
        └─ 4. Track content hash for change detection
```

## Transparent to the LLM Agent

The extension operates **entirely transparently**. The agent:

- **Does not know** the extension exists
- **Does not need modification** — it calls browser tools normally
- **Receives the same content shape** — cached content looks identical to live content
- **Only benefits** from lower latency and zero token cost on repeat visits

## Change Detection

Each page version is hashed (`simpleHash` — fast, deterministic, 32-bit):
- **Same hash** → content hasn't changed → re-caching is skipped
- **Different hash** → content changed → old entry is overwritten

This handles dynamic SPAs: if the page updates via AJAX, the next
`onCompleted` event triggers a re-cache.

## Integration Points

### Hermes TUI

No changes needed in Hermes. The TUI's browser tools (browser_navigate,
browser_snapshot) work with any browser — the extension just makes them faster.

The cache messages appear in the browser's background console,
not in Hermes logs. To verify caching is working:

```bash
# Check ToolRecall cache stats after navigation
toolrecall status
```

### VS Code Extension

The browser extension reuses the same HTTP proxy that the VS Code extension
spawns. If ToolRecall daemon + proxy are already running for VS Code,
the browser extension auto-discovers the port.

### Custom Agents

Any agent using browser tools via the Chrome DevTools Protocol (CDP)
benefits automatically. The extension intercepts navigation at the
browser level, below any agent framework.

## Proxy Port Discovery

The extension probes these ports in order: `8569, 8570, 8571, 8572`.

Default ToolRecall proxy port: **8569** (`toolrecall serve`).

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| No `[ToolRecall]` messages | Daemon not running | `toolrecall daemon && toolrecall serve` |
| `[ToolRecall] ToolRecall daemon not found` | Proxy port wrong | Check port with `toolrecall status` |
| Cache HIT but page not faster | Small pages | Normal — savings visible on 10K+ char pages |
| Content seems stale | Dynamic page | Page updated via AJAX — reload triggers re-cache |
| Extension icon shows no stats | No cache activity | Navigate to a page first |

## Test Vector

```bash
# Start ToolRecall
toolrecall daemon --foreground &
toolrecall serve --port 8569 &

# Open Chrome with the extension loaded
# Navigate to example.com
# First visit → cached (cache MISS → store)
# Second visit → cached (cache HIT → 0 tokens)
```

## Risks

| Risk | Impact | Mitigation |
|------|--------|------------|
| Dynamic pages (SPA, infinite scroll) | Stale cache on AJAX updates | Hash-based change detection; re-cache on every onCompleted |
| Auth-protected pages | Cached content captures sessions | Extension only stores what the browser loaded — inherits browser's session isolation |
| Cross-browser API differences | Safari lacks `webNavigation.onBeforeNavigate` in service worker context | Chromium + Firefox cover the primary use case. Safari falls back to `onCompleted` only caching |
| Daemon not running | Extension degrades gracefully | All fetches are try/catch — no user-facing errors |
| Large pages (100K+ chars) | Storage growth | SQLite with WAL mode; TTL-based cleanup via garbage_collect() |
