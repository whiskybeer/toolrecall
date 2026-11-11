# Forward Proxy — Cache LLM API Responses

The forward proxy intercepts HTTP requests to LLM providers, caching responses so repeat requests cost zero tokens. On cache hit the provider never receives the request.

**What it saves.** A hit needs a byte-identical request, so savings come from **repeat runs of the same task on unchanged code** (CI, evals), not interactive chat:

| Workload | Saving |
|----------|--------|
| Claude Code, identical rerun (5 SWE-bench Lite tasks, Sonnet) | **−77%** |
| Claude Code, cumulative after 4 runs | **−72%** |
| First run of any task | none |
| Interactive Claude Code session | ~0% |

Source: `bench/results/CLAUDE_CODE_INTEGRATIONS_REPORT.md`. A commit that changes files the agent reads stops the replay, and replays repeat failures (`toolrecall replay forget <run>`). The −36% on a 450-turn Hermes session in [Benchmark](BENCHMARK.md) comes from the Context Tracker, not from this proxy.

The proxy's savings are **additive to any provider's prefix caching** — it caches at the request level (identical request body → $0) while prefix caching discounts at the billing level (tokens already seen → lower rate). They work at different layers. No model lock-in.

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

### Claude Code (headless repeat runs)
Claude Code streams every request; streaming requests are cached only under a
canonicalization profile. Without it you get relay and usage logging, but 0 hits.
```bash
export ANTHROPIC_BASE_URL=http://localhost:8569
export ANTHROPIC_CUSTOM_HEADERS="X-ToolRecall-Canonicalize: claude-code"
```
Pin MCP servers (`--strict-mcp-config`) for stable keys; `toolrecall hook claude
ci-env` prints the full snippet. Anthropic streams (ending in `message_stop`, no
`[DONE]`) are stored and replayed verbatim like OpenAI-style streams; streams carrying
an `error` event are never cached. To see why a rerun stopped replaying, set
`TOOLRECALL_KEY_TRACE_DIR` on the proxy and run `toolrecall keytrace <dir>`. Scope and
measured limits: [CLAUDE_CODE_INTEGRATIONS.md](CLAUDE_CODE_INTEGRATIONS.md),
[AGENT_COMPATIBILITY.md](AGENT_COMPATIBILITY.md#claude-code).

**Cross-commit replay (new commit isolation):**
By default, Claude Code embeds `# gitStatus` (recent commit SHAs, branch) and `# userEmail` into turn 1, which causes new commits to fork the cache key even if the task is identical. To enable cross-commit replay:
- Profile suffix: `X-ToolRecall-Canonicalize: claude-code-nogit`
- Or header: `X-ToolRecall-Ignore-Git-Context: 1`
- Or env var: `TOOLRECALL_IGNORE_GIT_CONTEXT=1`

### Antigravity (headless repeat runs)
Antigravity injects volatile metadata (current local time in `<ADDITIONAL_METADATA>`,
conversation IDs, and brain UUIDs) into prompts. To replay headless Antigravity runs:
```bash
export OPENAI_BASE_URL=http://localhost:8569/v1  # or provider-specific base URL
# Pass header or set session default:
export TOOLRECALL_CANONICALIZE=antigravity
# or header: X-ToolRecall-Canonicalize: antigravity
```

> [!IMPORTANT]
> **Headless and SDK runs only — not interactive Antigravity chat.**
> The interactive Antigravity IDE and CLI chat UI (`agy`) routes model inference directly to Google with no HTTP base-URL override; ToolRecall cannot intercept or cache prompts in interactive Antigravity chat sessions. This canonical profile applies to headless evaluation harnesses, automated pipelines, and custom agents using the `google-antigravity` SDK or Gemini API routed through the proxy.

### Run Scoping & CI Failure Recovery (`replay forget`)
Tag proxy requests with a run ID via header or environment variable:
```bash
export TOOLRECALL_RUN_ID="ci-build-42"
# or header: X-ToolRecall-Run-Id: ci-build-42
```
If the test/job fails, evict all cached responses created during that run so retries execute live:
```bash
toolrecall replay forget "$TOOLRECALL_RUN_ID"
```
Alternatively, pass a key-trace directory: `toolrecall replay forget /tmp/trace`.

For individual fresh requests without evicting, send `X-ToolRecall-No-Cache: 1`.

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

Note: Streaming responses (SSE) are relayed chunk-by-chunk and do not get `Content-Length`. They are cached only under a canonicalization profile (`X-ToolRecall-Canonicalize`): the completed stream is stored verbatim and replayed as SSE.

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
| `TOOLRECALL_FORWARD_DEDUP` (or `[forward_proxy] dedup` in `toolrecall.toml`) | `false` | Request-body dedup — see below |
| `TOOLRECALL_FORWARD_BYPASS_SAMPLED` (or `[forward_proxy] bypass_sampled`) | `false` | Bypass cache for explicit `temperature > 0` |
| `TOOLRECALL_IGNORE_GIT_CONTEXT` | `false` | Normalize agent-injected git status / user email for cross-commit replay |
| `TOOLRECALL_DEDUP_NORMALIZE` | unset | Normalize timings/timestamps/temp paths/addresses before dedup hashing (automatic under the `claude-code` and `antigravity` profiles, which also set keep-first `protect_last=0`) |
| `TOOLRECALL_KEY_TRACE_DIR` | unset | Write canonical bodies + HIT/MISS of canonical-profile streaming requests for `toolrecall keytrace` (holds prompts; local only) |
| N/A | 5 MB | Maximum POST body size |

## Request-Body Dedup (opt-in)

Agents re-paste the same tool results / file reads into every turn's request. With
`dedup = true`, the proxy rewrites `/v1/chat/completions` POST bodies before
forwarding: duplicate large text blocks (≥ `TOOLRECALL_DEDUP_MIN_CHARS`, default
800) are replaced with a short stub pointing at the first occurrence. The first
copy is kept byte-exact, so provider-side prompt caching is unaffected; the
provider just never bills for the copies after the first.

- **Measured:** −32.3% prompt tokens / −30.0% billed cost on SWE-bench Lite × 8
  turns (billing-verified; see the README's [Input Dedup
  Hook](../README.md#input-dedup-hook) section).
- **Off by default.** The proxy forwards bodies byte-exact unless you opt in —
  request rewriting is a trust decision, not a cache lookup.
- **Per-request opt-out:** send `X-ToolRecall-No-Dedup: 1` (benchmark baseline
  arms use this header to measure both arms through the same proxy).
- **Never stubs the last N messages** (`TOOLRECALL_DEDUP_PROTECT_LAST`, default 2)
  — the model always sees the freshest context in full.
- **Fail-open:** any parse/rewrite error forwards the original body untouched.
- Scope: POST bodies ending in `/chat/completions` (OpenAI format, any host —
  OpenRouter's `/api/v1/...` included). Bodies that aren't JSON, have no
  `messages` list, or contain no stubbable duplicate pass through unchanged.
- Each stub event is logged: `DEDUP: stubbed N duplicate block(s), saved ~X chars`.
- **Per-tool attribution:** Run `toolrecall dedup-stats` to view which tools'
  outputs are being stubbed and the breakdown of saved tokens per tool. See
  [CLI Reference](CLI.md#toolrecall-dedup-stats).

## Stochastic Sampling Policy (opt-in)

By default, ToolRecall caches all byte-identical requests transparently, regardless
of `temperature`. For evaluation loops, self-consistency benchmarks, or best-of-N
sampling where repeated requests with explicit `temperature > 0` require fresh,
independent random draws from the provider:

- **Enable globally:** Set `bypass_sampled = true` in `[forward_proxy]` (or env
  `TOOLRECALL_FORWARD_BYPASS_SAMPLED=1`).
- **Per-request opt-in to bypass:** Send `X-ToolRecall-Bypass-Sampled: 1` header.
- **Per-request opt-in to cache:** When bypass is globally enabled, send
  `X-ToolRecall-Cache-Sampled: 1` to force caching a sampled request (e.g. for exact
  eval replay).
- Requests with greedy temperature (`temperature == 0`) or omitted temperature
  continue to be cached transparently.

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