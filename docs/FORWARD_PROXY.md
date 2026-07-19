# Forward Proxy — Cache LLM API Responses

The forward proxy intercepts HTTP requests to LLM providers, caching responses so repeat requests cost zero tokens. On cache hit the provider never receives the request.

## How It Works

1. **Point any SDK** to `http://localhost:8569` (set `base_url` or env var)
2. Proxy **routes to the correct upstream** by detecting the API key prefix from the `Authorization` header
3. On **cache miss**: forwards to the real API, stores response, returns it
4. On **cache hit**: returns cached response directly — no API call, zero tokens

## Agent-Agnostic Setup (one-time)

Set these env vars so every tool on the machine routes through the proxy:

```bash
# In ~/.profile, ~/.bashrc, or /etc/environment
export OPENAI_BASE_URL=http://localhost:8569/v1
export ANTHROPIC_BASE_URL=http://localhost:8569
```

Any agent (Hermes, Aider, OpenCode, curl) that reads these env vars will transparently route through the proxy.

For **Hermes Agent** specifically:

```bash
hermes config set model.base_url http://localhost:8569/v1
```

## Intelligent Routing by API Key

Because multiple providers share the same OpenAPI-compatible path (`/v1/chat/completions`), the proxy determines the real upstream from the **Authorization header**:

| API Key Prefix | Routes To |
|----------------|-----------|
| `Bearer sk-or-*` | `openrouter.ai` |
| `Bearer sk-ant-*` | `api.anthropic.com` |
| `Bearer xai-*` | `api.x.ai` |
| `Bearer sk-*` (real OpenAI key) | `api.openai.com` |

This also works for providers that map to OpenAI by default via path-based routing — the proxy overrides based on the actual key.

## Path Rewriting

The proxy automatically rewrites paths for providers that use non-standard API paths:

| Provider | SDK sends | Proxy rewrites to |
|----------|-----------|-------------------|
| OpenRouter | `/v1/chat/completions` | `/api/v1/chat/completions` |
| OpenRouter | `/v1/models` | `/api/v1/models` |

The SDK sends standard OpenAI-compatible paths. The proxy rewrites them before forwarding to the upstream.

## Usage

### OpenAI-compatible SDKs
```bash
export OPENAI_BASE_URL=http://localhost:8569/v1
```

### Anthropic SDK
```bash
export ANTHROPIC_BASE_URL=http://localhost:8569
```

### Direct HTTP
```bash
# First call: MISS, forwards to upstream, caches response
curl -X POST http://localhost:8569/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer sk-or-..." \
  -d '{"model":"gpt-4","messages":[{"role":"user","content":"hello"}]}'

# Second call with identical body: HIT, returned from cache — zero tokens
curl -X POST http://localhost:8569/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer sk-or-..." \
  -d '{"model":"gpt-4","messages":[{"role":"user","content":"hello"}]}'
```

## Supported Providers

| Provider | Host | Routing Method | Status |
|----------|------|----------------|--------|
| OpenAI | `api.openai.com` | Path + key | ✅ |
| Anthropic | `api.anthropic.com` | Path + key | ✅ |
| Google Gemini | `generativelanguage.googleapis.com` | Path | ✅ |
| DeepSeek | `api.deepseek.com` | Path + key | ✅ |
| xAI | `api.x.ai` | Path + key | ✅ |
| Mistral | `api.mistral.ai` | Path + key | ✅ |
| Groq | `api.groq.com` | Path + key | ✅ |
| Together | `api.together.xyz` | Path + key | ✅ |
| OpenRouter | `openrouter.ai` | Key override + path rewrite | ✅ |

Requests to hosts not in this list are forwarded uncached (no caching).

### Content-Length Fix

Proxy responses now include `Content-Length`. Previously, `resp.read()` consumed the upstream body (handling chunked transfer encoding), but the response was sent without `Content-Length` and with `Connection: keep-alive`. HTTP/1.0 clients (Python `http.client`, some SDKs) hung indefinitely waiting for the body boundary. Now every non-streaming response includes `Content-Length`.

Note: Streaming responses (SSE for LLM chat completions) bypass caching entirely and are relayed chunk-by-chunk — they do not get `Content-Length`.

## Cache Key

Cache key = `SHA256(method + host + path + SHA256(body))`

Same request body + same endpoint = same cache key. A different request always produces a different key and hits the provider.

## Token Savings

The forward proxy saves **all** tokens on a cache hit — input + output. Because the request never reaches the provider, no tokens are billed.

Compare with file/terminal caching, which only saves input tokens (the output still enters the LLM context).

## Configuration

| Var | Default | Description |
|-----|---------|-------------|
| `TOOLRECALL_FORWARD_PORT` | `8569` | Proxy listen port |
| `TOOLRECALL_FORWARD_TIMEOUT` | `30` | Upstream request timeout (seconds) |
| `TOOLRECALL_FORWARD_STREAM_TIMEOUT` | `300` | Streaming request timeout |
| N/A | 5 MB | Maximum POST body size |

## Starting

The proxy starts **automatically** with `toolrecall daemon`. It is part of the daemon process.

To run standalone:
```bash
toolrecall serve                 # default port 8569
toolrecall serve --port 9000     # custom port
```

## Security

- Binds to `127.0.0.1` only — no network exposure
- Never falls back to plaintext HTTP for known API hosts
- Authorization headers are preserved and forwarded to the real API
- Cache database (`~/.toolrecall/cache.db`) is chmod 600