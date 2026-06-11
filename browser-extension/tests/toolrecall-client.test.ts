import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { ToolRecallClient } from '../src/toolrecall-client';

describe('ToolRecallClient', () => {
  it('rejects invalid ports', () => {
    expect(() => new ToolRecallClient(0)).toThrow('Invalid port');
    expect(() => new ToolRecallClient(65536)).toThrow('Invalid port');
    expect(() => new ToolRecallClient(-1)).toThrow('Invalid port');
  });

  it('accepts valid ports', () => {
    const client = new ToolRecallClient(8569);
    expect(client).toBeDefined();
    // Private host field — accessible via internal
    expect((client as any).host).toBe('127.0.0.1:8569');
  });

  it('accepts edge case port 1 and 65535', () => {
    expect(() => new ToolRecallClient(1)).not.toThrow();
    expect(() => new ToolRecallClient(65535)).not.toThrow();
  });

  it('discoverPort returns null when no proxy running', async () => {
    // Mock fetch to always fail
    const originalFetch = globalThis.fetch;
    globalThis.fetch = vi.fn().mockRejectedValue(new Error('connection refused'));

    const port = await ToolRecallClient.discoverPort();
    expect(port).toBeNull();

    globalThis.fetch = originalFetch;
  });

  it('discoverPort finds proxy on expected port', async () => {
    const originalFetch = globalThis.fetch;
    let callCount = 0;

    // Fail on first port (8569), succeed on second (8570)
    globalThis.fetch = vi.fn().mockImplementation(async (url: string) => {
      callCount++;
      if (url.includes('8570')) {
        return {
          ok: true,
          json: async () => ({ status: 'ok', daemon: 'connected' }),
        };
      }
      throw new Error('no connection');
    });

    const port = await ToolRecallClient.discoverPort();
    expect(port).toBe(8570);
    expect(callCount).toBeGreaterThanOrEqual(2);

    globalThis.fetch = originalFetch;
  });

  it('checkCache returns null on fetch failure', async () => {
    const originalFetch = globalThis.fetch;
    globalThis.fetch = vi.fn().mockRejectedValue(new Error('timeout'));

    const client = new ToolRecallClient(8569);
    const result = await client.checkCache({
      url: 'https://example.com',
      contentType: 'snapshot',
    });
    expect(result).toBeNull();

    globalThis.fetch = originalFetch;
  });

  it('checkCache returns parsed response on success', async () => {
    const originalFetch = globalThis.fetch;
    globalThis.fetch = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ cached: true, content: '<html/>', tokens_saved: 42 }),
    });

    const client = new ToolRecallClient(8569);
    const result = await client.checkCache({
      url: 'https://example.com',
      contentType: 'snapshot',
    });
    expect(result).toEqual({ cached: true, content: '<html/>', tokens_saved: 42 });

    globalThis.fetch = originalFetch;
  });

  it('storeCache returns false on fetch failure', async () => {
    const originalFetch = globalThis.fetch;
    globalThis.fetch = vi.fn().mockRejectedValue(new Error('connection lost'));

    const client = new ToolRecallClient(8569);
    const result = await client.storeCache(
      { url: 'https://example.com', contentType: 'html' },
      '<html/>',
    );
    expect(result).toBe(false);

    globalThis.fetch = originalFetch;
  });

  it('storeCache returns true on success', async () => {
    const originalFetch = globalThis.fetch;
    globalThis.fetch = vi.fn().mockResolvedValue({ ok: true });

    const client = new ToolRecallClient(8569);
    const result = await client.storeCache(
      { url: 'https://example.com', contentType: 'html' },
      '<html/>',
    );
    expect(result).toBe(true);

    globalThis.fetch = originalFetch;
  });

  it('builds correct cache key for URLs', () => {
    const client = new ToolRecallClient(8569);
    const key = (client as any).buildCacheKey({
      url: 'https://example.com/page',
      contentType: 'snapshot',
    });
    expect(key).toBe('browser:page:https___example_com_page:snapshot');
  });

  it('builds cache key with ref', () => {
    const client = new ToolRecallClient(8569);
    const key = (client as any).buildCacheKey({
      url: 'https://example.com',
      contentType: 'html',
      ref: 'e42',
    });
    expect(key).toBe('browser:page:https___example_com:html:e42');
  });
});
