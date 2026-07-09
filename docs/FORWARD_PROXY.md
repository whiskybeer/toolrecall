# Forward Proxy — Cache LLM API Responses

The forward proxy intercepts HTTP requests to LLM providers by matching the `Host` header. On repeat requests with identical bodies, it returns the cached response — **zero tokens consumed, the provider never receives the request**.

## How It Works

1. Point your SDK's base URL to `http://localhost:8569`
2. The proxy hashes the request body (SHA-256)
3. On **cache hit**: returns the cached response — no API call
4. On **cache miss**: forwards to the real API, stores the response, returns it

All caching is done by the ToolRecall daemon via UDS. The proxy is a thin HTTP→UDS adapter.

## Usage

### OpenAI-compatible SDKs
```bash
export OPENAI_BASE_URL=http://localhost:8569/v1
# Or in code:
# openai.base_url = "http://localhost:8569/v1"
```

### Anthropic SDK
```bash
export ANTHROPIC_BASE_URL=http://localhost:8569
# Or in code:
# anthropic.Anthropic(base_url="http://localhost:8569")
```

### Google / DeepSeek / any other provider
Set the SDK's `base_url` to `http://localhost:8569`. The proxy routes by the `Host` header, so it works with any provider in the supported list.

### Direct HTTP
```bash
# Cache hit example (same request twice)
curl -X POST http://localhost:8569/v1/chat/completions \
  -H "Host: api.openai.com" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $OPENAI_API_KEY" \
  -d '{"model":"gpt-4","messages":[{"role":"user","content":"hello"}]}'
# First call: MISS, forwards to OpenAI
# Second call: HIT, returns instantly — zero tokens consumed
```

## Supported Providers

The proxy currently caches responses for these hosts:

| Provider | Host | Status |
|----------|------|--------|
| OpenAI | `api.openai.com` | ✅ |
| Anthropic | `api.anthropic.com` | ✅ |
| Google Gemini | `generativelanguage.googleapis.com` | ✅ |
| DeepSeek | `api.deepseek.com` | ✅ |
| xAI | `api.x.ai` | ✅ |
| Mistral | `api.mistral.ai` | ✅ |
| Groq | `api.groq.com` | ✅ |
| Together | `api.together.xyz` | ✅ |
| OpenRouter | `openrouter.ai` | ✅ |

Requests to hosts not in this list are forwarded uncached.

## Cache Key

Cache key = `SHA256(method + host + path + SHA256(body))`

Same request body + same endpoint = same cache key. A different request (different model, different messages, different temperature) always produces a different key and hits the provider.

## Token Savings

The forward proxy saves **all** tokens on a cache hit — input + output. Because the request never reaches the provider, no tokens are billed. This is different from the file/terminal cache, which only saves input tokens (the output still enters the LLM context).

The file/terminal cache and the forward proxy are **independent** — you can enable one without the other.

## Starting

The forward proxy starts automatically with `toolrecall daemon`. To run it standalone:

```bash
toolrecall serve          # default port 8569
toolrecall serve --port 9000  # custom port
```

## Configuration

| Var | Default | Description |
|-----|---------|-------------|
| `TOOLRECALL_FORWARD_PORT` | `8569` | Port for the forward proxy |
| N/A | 5 MB | Maximum POST body size (rejects larger) |
| N/A | 300s | Cache TTL for API responses |

## Security

- Binds to `127.0.0.1` only — no network exposure
- Never falls back to plaintext HTTP for known API hosts
- Authorization headers are preserved and forwarded to the real API
- The cache database (`~/.toolrecall/cache.db`) is chmod 600