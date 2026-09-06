"""ToolRecall Forward Proxy — cache API responses without touching the provider.

Intercepts HTTP requests to LLM providers (OpenAI, Anthropic, Google, DeepSeek, etc.)
by matching the Host header. On repeat requests with identical bodies, returns the
cached response — no API call, no token cost.

Architecture:
    Agent SDK → Forward Proxy (port 8569, set OPENAI_BASE_URL / ANTHROPIC_BASE_URL)
      → Cache HIT: respond from api_cache table
      → Cache MISS: forward to real API, store response, return

No MITM needed — works by redirecting the base URL in your SDK config,
preserving all original headers (Authorization, Content-Type) and body intact.

Usage:
    export OPENAI_BASE_URL=http://localhost:8569/v1
    export ANTHROPIC_BASE_URL=http://localhost:8569
    # Or set any SDK's base_url to http://localhost:8569
    # Then use your agent/scripts normally — API responses are cached automatically.
"""

import hashlib
import http.client
import http.server
import json
import logging
import os
import re
import sys
import threading
import time
from socketserver import ThreadingMixIn

from toolrecall.transport import TransportClient

log = logging.getLogger("toolrecall.proxy")

# Maximum POST body size (5 MB) — prevents OOM from malicious payloads
# or misconfigured clients sending multi-GB blobs to a localhost process.
MAX_BODY_SIZE = 5 * 1024 * 1024

# Regex to detect streaming requests in JSON body.
# Matches "stream": true with any whitespace (including none) around the colon.
# Catches canonical JSON ({"stream": true}), compact ({"stream":true}),
# and any whitespace variation providers might send.
_STREAM_RE = re.compile(rb'"stream"\s*:\s*true')

# ── Body canonicalization ────────────────────────────────────────────────────
# Volatile patterns observed in Warp's server-side harness bodies (live
# capture, 2026-09-03). Each is matched against the DECODED text of message
# content fields; replacing them must be idempotent and must never touch
# semantics the model actually needs.
#
# _RE_CANON_* patterns are applied to the canonicalized body text:
#  1. UUIDs (v4-shaped, used as current_run_id) → fixed placeholder
#  2. ISO-8601 UTC timestamps (current_time fields) → fixed placeholder
#  3. CR characters in terminal-context lines → stripped (PowerShell echo
#     variance: the same command line reaches Turn-0 with \r\n or \n
#     depending on shell/terminal version; live capture 2026-09-04 showed
#     a single \r changing the canon key across otherwise-identical runs)
_RE_CANON_UUID = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)
_RE_CANON_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z\b")
_RE_CTX_SECTION = re.compile(r"# Conversation context.*?(?=\n# )", re.S)
_RE_DANGLING_COMMA = re.compile(r",(\s*\})")
_RE_LEADING_COMMA = re.compile(r"\{\s*,")
_RE_SPACE_AFTER_COLON = re.compile(r":[ \t]+")
_RE_INDENT_EMPTY = re.compile(r"\n[ \t]+")


def _strip_env_objects(text):
    for key in ("directory_state", "shell", "operating_system"):
        text = re.sub(r'"%s"\s*:\s*\{[^{}]*\}\s*,?\s*' % key, "", text)
    text = _RE_DANGLING_COMMA.sub(r"\1", text)
    text = _RE_LEADING_COMMA.sub("{", text)
    text = _RE_SPACE_AFTER_COLON.sub(": ", text)
    text = _RE_INDENT_EMPTY.sub("\n", text)
    return text


def _collapse_named_arrays(text):
    """Replace embedded JSON arrays of {name,...} objects (skills lists) with
    a placeholder. Balance-scan handles quotes/escapes; arrays that fail the
    {name}-check are kept verbatim."""
    out, last = [], 0
    k = 0
    while True:
        j = text.find("[", k)
        if j == -1:
            break
        if '"name"' in text[j : j + 600]:
            depth, p, end, in_str = 0, j, None, False
            while p < len(text):
                ch = text[p]
                if in_str:
                    if ch == "\\":
                        p += 2
                        continue
                    if ch == '"':
                        in_str = False
                else:
                    if ch == '"':
                        in_str = True
                    elif ch == "[":
                        depth += 1
                    elif ch == "]":
                        depth -= 1
                        if depth == 0:
                            end = p
                            break
                p += 1
            if end is not None:
                block = text[j : end + 1]
                try:
                    arr = json.loads(block)
                    ok = (
                        isinstance(arr, list)
                        and arr
                        and all(isinstance(x, dict) and "name" in x for x in arr)
                    )
                except Exception:
                    ok = False
                out.append(text[last:j])
                if ok:
                    out.append("[[toolrecall-skills-placeholder]]")
                    last = end + 1
                else:
                    out.append(text[j : end + 1])
                    last = end + 1
                k = end + 1
                continue
        k = j + 1
    out.append(text[last:])
    return "".join(out)


def _canon_text(text):
    # CR normalization FIRST (idempotent): PowerShell/terminal echo reaches
    # Turn-0 bodies with \r\n or \n depending on shell version; a lone \r
    # must not fork the canon key (live bugfix-exp capture 2026-09-04).
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    # Collapse blank-line runs (2+ newlines) to one: blank-line composition
    # inside pasted prompts is a clipboard/terminal artifact, never semantics
    # (live capture seq 54 vs 61: "\n\n\n" vs "\n\n\n\n" forked the key).
    text = re.sub(r"\n{2,}", "\n", text)
    text = _RE_CANON_UUID.sub("00000000-0000-4000-8000-canonuuidplaceholder", text)
    text = _RE_CANON_TIMESTAMP.sub("1970-01-01T00:00:00Z", text)
    text = _RE_CTX_SECTION.sub("", text)
    text = _strip_env_objects(text)
    text = _collapse_named_arrays(text)
    return text


def _canon_walk(node):
    if isinstance(node, dict):
        return {k: _canon_walk(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_canon_walk(x) for x in node]
    if isinstance(node, str):
        return _canon_text(node)
    return node


def _canonicalize_body(profile: str, body: bytes):
    """Canonicalized body for cache-key hashing under `profile`, or None.

    Profile `warp`: strips volatile fields observed in Warp's server-side
    harness (run UUIDs, timestamps, session context section, ephemeral env
    objects, skill-list arrays — live capture 2026-09-03). Envelope is parsed
    once and re-serialized with sorted keys; only the cache key uses the
    canonical form — the provider always receives the original body."""
    if profile != "warp":
        return None
    try:
        envelope = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    envelope = _canon_walk(envelope)
    return json.dumps(envelope, sort_keys=True, ensure_ascii=False).encode("utf-8")


# Upstream HTTPS connection timeout. Configurable via TOOLRECALL_FORWARD_TIMEOUT
# env var (seconds). Default: 30s for normal API calls, 300s for streaming SSE.
_FORWARD_TIMEOUT = int(os.environ.get("TOOLRECALL_FORWARD_TIMEOUT", "30"))
_FORWARD_STREAM_TIMEOUT = int(os.environ.get("TOOLRECALL_FORWARD_STREAM_TIMEOUT", "300"))

# ─── Resilience (retry + circuit breaker) — opt-in, lazy singletons ─────────

_forward_breaker = None  # per-process CircuitBreaker (all allowlisted hosts)


def _resilience_flag(key: str) -> bool:
    """Read a boolean [resilience] config key (default off)."""
    try:
        from toolrecall.config import load_config

        val = load_config().get("resilience", key, default=False)
        if isinstance(val, str):
            return val.strip().lower() in ("1", "true", "yes", "on")
        return bool(val)
    except Exception:
        return False


def _resilience_num(key: str, default: float) -> float:
    """Read a numeric [resilience] config key."""
    try:
        from toolrecall.config import load_config

        return float(load_config().get("resilience", key, default=default))
    except Exception:
        return default


# ─── Usage Measurement Log ──────────────────────────────────────────────────
# Records actual token usage (from API response "usage" field) on every proxy
# request — works for ANY agent using the proxy, not just Hermes.
#
# Columns:
#   timestamp         float (unix epoch)
#   cache_status      HIT | MISS | STREAM
#   target_host       e.g. openrouter.ai
#   target_path       e.g. /v1/chat/completions
#   request_hash      first 16 hex chars of cache key
#   prompt_tokens     actual prompt tokens in the request (from provider usage)
#   completion_tokens actual completion tokens in the response
#   cache_read_tokens provider's prefix-cache read tokens (if reported)
#   cache_write_tokens provider's prefix-cache write tokens (if reported)
#
# Security: contains NO request bodies, NO response content, NO
# API keys, NO model output. Only integers and routing metadata.
# The request_hash is a SHA-256 digest — not the request content.
#
# Query examples:
#   Actual tokens sent to LLM:
#     SELECT SUM(prompt_tokens) FROM proxy_usage WHERE cache_status IN ('MISS','STREAM')
#   Tokens saved by proxy cache replay:
#     SELECT SUM(prompt_tokens) FROM proxy_usage WHERE cache_status = 'HIT'
#   Provider prefix-cache effectiveness:
#     SELECT SUM(cache_read_tokens), SUM(cache_write_tokens) FROM proxy_usage

_USAGE_LOG_PATH = os.path.expanduser("~/.toolrecall/proxy_usage.csv")
_USAGE_LOG_HEADER = "timestamp,cache_status,target_host,target_path,request_hash,prompt_tokens,completion_tokens,cache_read_tokens,cache_write_tokens\n"
_USAGE_LOG_LOCK = threading.Lock()


def _init_usage_log():
    """Ensure the usage CSV exists with headers (one-time, thread-safe)."""
    os.makedirs(os.path.dirname(_USAGE_LOG_PATH), exist_ok=True)
    if not os.path.exists(_USAGE_LOG_PATH):
        with open(_USAGE_LOG_PATH, "w") as f:
            f.write(_USAGE_LOG_HEADER)


def _try_parse_usage(body: str) -> dict:
    """Best-effort parse of 'usage' from API response JSON. Never raises.

    All major providers (OpenAI, Anthropic, DeepSeek, OpenRouter, Google,
    xAI, Mistral, Groq, Together) report usage in the OpenAI-compatible
    ``usage`` field format within chat completions responses.

    Provider-specific field names:
      - cache_read_input_tokens (OpenAI, compatible providers)
      - cache_read_tokens (Anthropic)
      - cache_creation_input_tokens (OpenAI, compatible)
      - cache_write_tokens (Anthropic)
    """
    try:
        return json.loads(body).get("usage", {})
    except (json.JSONDecodeError, AttributeError, TypeError):
        return {}


def _csv_escape(s: str) -> str:
    """Escape a string for CSV — replace commas and newlines that would break columns."""
    return s.replace(",", ";").replace("\n", " ").replace("\r", " ")


def _log_proxy_usage(
    cache_status: str,
    target_host: str,
    target_path: str,
    request_hash: str,
    body: str = "",
    prompt_tokens_override: int = 0,
) -> None:
    """Append one usage row to the proxy_usage.csv log.

    Thread-safe via lock. Best-effort — never raises, never blocks
    the proxy response path.

    Args:
        cache_status: HIT | MISS | STREAM
        target_host: upstream API hostname
        target_path: upstream API path (e.g. /v1/chat/completions)
        request_hash: SHA-256 cache key (first 16 chars logged)
        body: response body to extract usage from (optional)
        prompt_tokens_override: if > 0, use this instead of parsing (for STREAM)
    """
    usage = _try_parse_usage(body)
    pt = (
        prompt_tokens_override
        if prompt_tokens_override > 0
        else (usage.get("prompt_tokens", 0) or 0)
    )
    ct = usage.get("completion_tokens", 0) or 0
    crt = usage.get("cache_read_input_tokens", 0) or usage.get("cache_read_tokens", 0) or 0
    cwt = usage.get("cache_creation_input_tokens", 0) or usage.get("cache_write_tokens", 0) or 0
    ts = time.time()
    with _USAGE_LOG_LOCK:
        try:
            with open(_USAGE_LOG_PATH, "a") as f:
                f.write(
                    f"{ts:.3f},{cache_status},{_csv_escape(target_host)}"
                    f",{_csv_escape(target_path)},{_csv_escape(request_hash[:16])}"
                    f",{pt},{ct},{crt},{cwt}\n"
                )
        except OSError:
            pass  # best-effort — never break the proxy over a log write


# Ensure log file exists at import time
_init_usage_log()


# ─── Constants: known API hosts ─────────────────────────────────────────────

# Known LLM API hosts that the forward proxy routes requests for.
FORWARD_HOSTS = {
    "api.openai.com",
    "api.anthropic.com",
    "generativelanguage.googleapis.com",
    "api.deepseek.com",
    "api.x.ai",
    "api.mistral.ai",
    "api.groq.com",
    "api.together.xyz",
    "openrouter.ai",
}


def _host_allowed(host: str) -> bool:
    """SSRF guard — only known, trusted LLM API hosts may be forwarded to.

    target_host is derived from caller-controlled headers (Host /
    X-Target-Host), so every outbound connection must be checked against this
    allowlist first. This prevents using the localhost proxy as a relay to
    cloud-metadata endpoints or internal services (py/full-ssrf).
    """
    return host.split(":", 1)[0] in FORWARD_HOSTS


# Path-based routing: maps distinctive path prefixes to API hosts.
# Used when the SDK sends Host: localhost (OPENAI_BASE_URL=http://localhost:8569).
# Ordered by specificity — more specific paths checked first.
# /v1beta (Google) must be checked before /v1 to avoid matching /v1/... wrongly.
PATH_ROUTES: list[tuple[str, str]] = [
    ("generativelanguage.googleapis.com", "/v1beta"),
    ("api.anthropic.com", "/v1/messages"),
    ("api.anthropic.com", "/v1/complete"),
    ("api.openai.com", "/v1/chat/completions"),
    ("api.openai.com", "/v1/embeddings"),
    ("api.openai.com", "/v1/models"),
    ("api.openai.com", "/v1/images"),
    ("api.openai.com", "/v1/audio"),
    ("api.openai.com", "/v1/moderations"),
    ("api.openai.com", "/v1/files"),
    ("api.openai.com", "/v1/fine_tuning"),
    ("api.openai.com", "/v1/assistants"),
    ("api.openai.com", "/v1/threads"),
    ("api.openai.com", "/v1/vector_stores"),
    ("api.openai.com", "/v1/batches"),
    ("api.openai.com", "/v1/organization"),
    ("api.openai.com", "/v1/projects"),
    ("api.openai.com", "/v1/realtime"),
    ("api.openai.com", "/v1/responses"),
    ("api.deepseek.com", "/v1/chat/completions"),
    ("api.deepseek.com", "/v1/models"),
    ("api.deepseek.com", "/v1/user"),
    ("api.deepseek.com", "/v1/dashboard"),
    ("api.x.ai", "/v1/chat/completions"),
    ("api.x.ai", "/v1/embeddings"),
    ("api.x.ai", "/v1/models"),
    ("api.mistral.ai", "/v1/chat/completions"),
    ("api.mistral.ai", "/v1/embeddings"),
    ("api.mistral.ai", "/v1/models"),
    ("api.mistral.ai", "/v1/fim"),
    ("api.mistral.ai", "/v1/agents"),
    ("api.mistral.ai", "/v1/files"),
    ("api.groq.com", "/v1/chat/completions"),
    ("api.groq.com", "/v1/embeddings"),
    ("api.groq.com", "/v1/models"),
    ("api.groq.com", "/v1/audio"),
    ("api.together.xyz", "/v1/chat/completions"),
    ("api.together.xyz", "/v1/embeddings"),
    ("api.together.xyz", "/v1/models"),
    ("api.together.xyz", "/v1/images"),
    ("api.together.xyz", "/v1/files"),
    ("openrouter.ai", "/v1/chat/completions"),
    ("openrouter.ai", "/v1/models"),
    # Fallback /v1 for any provider not listed above (e.g. custom endpoints)
    ("api.openai.com", "/v1"),
    ("api.anthropic.com", "/v1"),
    ("api.deepseek.com", "/v1"),
    ("api.x.ai", "/v1"),
    ("api.mistral.ai", "/v1"),
    ("api.groq.com", "/v1"),
    ("api.together.xyz", "/v1"),
    ("openrouter.ai", "/v1"),
]


class ForwardProxyHandler(http.server.BaseHTTPRequestHandler):
    """Forward proxy handler that caches API responses via ToolRecall daemon.

        Receives requests pointed at this proxy via OPENAI_BASE_URL / ANTHROPIC_BASE_URL
        or by setting any SDK's base URL to http://localhost:8569.
        Matches the Host header against FORWARD_HOSTS, hashes the request
        body, checks the api_cache, and either returns cached responses
        or forwards to the real API and caches the result.

    No MITM needed — works by URL redirection, preserving
    all original headers (Authorization, Content-Type) and body intact.
    """

    def __init__(self, *args, **kwargs):
        self._client = TransportClient()
        self._usage_prompt_tokens_fallback = 0
        super().__init__(*args, **kwargs)

    # ── Generic dispatch ────────────────────────────────

    def _handle(self, method: str):
        """Handle any HTTP method (GET, POST, etc.) via forwarding proxy.

        Resolves the real target host from:
          1. X-Target-Host header (explicit override, for SDK usage)
          2. Host header (works with curl -H "Host: api.openai.com")
          3. Path-based routing: /v1/chat/completions -> api.openai.com
          4. Authorization header override: API key prefix tells us the real provider
        """
        target_host = self.headers.get("X-Target-Host") or self.headers.get("Host", "")
        if target_host is None:
            target_host = ""
        target_path = self.path

        # Path-based routing fallback: when Host is localhost (SDK redirect),
        # infer the real API host from the path prefix.
        if not target_host or target_host.split(":")[0] in ("localhost", "127.0.0.1"):
            for known_host, path_prefix in PATH_ROUTES:
                if target_path.startswith(path_prefix):
                    target_host = known_host
                    break

            # Header-based routing: API key prefix tells us the real provider.
            # This overrides path-based routing for any path — essential for
            # providers that reuse OpenAI-compatible paths (OpenRouter, xAI, etc.)
            auth = self.headers.get("Authorization", "")
            if auth.startswith("Bearer sk-or-"):
                target_host = "openrouter.ai"
            elif auth.startswith("Bearer sk-ant-"):
                target_host = "api.anthropic.com"
            elif auth.startswith("Bearer xai-"):
                target_host = "api.x.ai"
            # Legacy Anthropic tiebreaker for x-api-key / anthropic-version headers
            elif target_host == "api.openai.com" and target_path in (
                "/v1/models",
                "/v1/embeddings",
                "/v1/files",
            ):
                anthro_key = self.headers.get("x-api-key", "")
                anthro_version = self.headers.get("anthropic-version", "")
                if anthro_version or (anthro_key and not auth.startswith("Bearer ")):
                    target_host = "api.anthropic.com"

        # Path rewrite: OpenRouter API lives under /api/v1, not /v1
        if target_host == "openrouter.ai" and target_path.startswith("/v1"):
            target_path = "/api" + target_path

        target_scheme = "https"

        # SECURITY (py/full-ssrf): target_host is built from caller-controlled
        # headers (Host / X-Target-Host). Enforce the FORWARD_HOSTS allowlist
        # BEFORE any outbound connection so a caller can't use this localhost
        # proxy as an SSRF relay to cloud-metadata or internal services.
        if not _host_allowed(target_host):
            log.warning("Blocked SSRF attempt — non-allowlisted target host %r", target_host)
            self.send_response(403)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error":"Forbidden: non-allowlisted target host"}')
            return

        body_bytes = b""
        content_length = int(self.headers.get("Content-Length", 0))
        is_streaming = False
        if content_length > 0:
            if content_length > MAX_BODY_SIZE:
                self.send_response(413)
                self.end_headers()
                self.wfile.write(b'{"error":"Request too large"}')
                return
            body_bytes = self.rfile.read(content_length)
            # Detect streaming requests — bypass cache, use chunked relay
            if body_bytes and _STREAM_RE.search(body_bytes):
                is_streaming = True

        # Estimate prompt tokens from request body (fallback when response
        # body has no usage field — e.g. cached HIT or malformed response).
        self._usage_prompt_tokens_fallback = max(1, len(body_bytes) // 4)

        # ── Canonicalization (X-ToolRecall-Canonicalize: <profile>) ─────────
        # Agent harnesses inject per-request volatile fields (run UUIDs,
        # timestamps, ephemeral env state) into message content. Byte-hashing
        # then yields a different key for semantically identical requests —
        # measured live against Warp's server-side harness (2026-09-03):
        # two identical tasks differed in 4 places, hit rate 0%.
        # Canonicalization strips/replaces those fields BEFORE hashing so
        # replay fires for identical semantics. The upstream provider still
        # receives the ORIGINAL body — only the cache key is canonicalized.
        # Note: canonical replays serve the cached response for a body that
        # differs at the stripped positions only; correctness rests on those
        # positions being genuinely non-semantic.
        canonical_profile = (self.headers.get("X-ToolRecall-Canonicalize") or "").strip().lower()
        hash_body = body_bytes
        if canonical_profile and body_bytes:
            canonical = _canonicalize_body(canonical_profile, body_bytes)
            if canonical is not None:
                hash_body = canonical

        # Build cache key: hash(method + host + path + body)
        body_hash = hashlib.sha256(hash_body).hexdigest()
        request_str = f"{method}:{target_host}:{target_path}:{body_hash}"
        request_hash = hashlib.sha256(request_str.encode()).hexdigest()

        # Streaming requests: bypass cache entirely, use chunked passthrough
        # Opt-out header: X-ToolRecall-No-Cache skips BOTH the HIT lookup and
        # the store. Baseline arms in benchmarks use this to bill every pass.
        no_cache = (self.headers.get("X-ToolRecall-No-Cache") or "").strip() in (
            "1",
            "true",
            "yes",
        )
        if is_streaming:
            # Stream cache (canonical profiles): streaming requests normally
            # bypass the cache, but Warp's harness ALWAYS streams — so without
            # this the api_cache is dead behind Warp (measured 2026-09-03:
            # 0% hits despite canonical keys matching). For canonical-profile
            # requests we (a) serve cache HITs as synthesized SSE and
            # (b) tee MISS streams into a non-stream cache entry.
            if canonical_profile:
                cached = self._client.send(
                    {"cmd": "cached_api_check", "request_hash": request_hash}
                )
                if cached.get("cached") and 200 <= cached.get("status", 200) < 300:
                    n = self._replay_stream_from_cache(
                        cached, target_host, target_path, request_hash
                    )
                    if n is not None:
                        _log_proxy_usage(
                            "HIT", target_host, target_path, request_hash, prompt_tokens_override=n
                        )
                        return  # replay written
                # MISS (or replay failed) → fall through to live streaming,
                # which will tee the response into the cache.

            log.info(
                "STREAM: %s %s%s — %s",
                method,
                target_host,
                target_path,
                "cache miss, tee-to-cache" if canonical_profile else "bypassing cache",
            )
            self._forward_streaming(
                method,
                target_host,
                target_path,
                target_scheme,
                body_bytes,
                canonical_profile=canonical_profile,
                request_hash=request_hash,
                body_for_tee=body_bytes,
            )
            return

        if no_cache:
            log.info(
                "NO-CACHE: %s %s%s — opt-out header, forwarding directly",
                method,
                target_host,
                target_path,
            )
            resp_status, resp_headers, resp_body = self._forward(
                method,
                target_host,
                target_path,
                target_scheme,
                body_bytes,
            )
            _log_proxy_usage(
                "STREAM",  # counted as cache-ineligible, like streaming
                target_host,
                target_path,
                request_hash,
                resp_body.decode("utf-8", "replace"),
            )
            resp_body_bytes = (
                resp_body if isinstance(resp_body, bytes) else resp_body.encode("utf-8")
            )
            self.send_response(resp_status)
            self.send_header("Content-Length", str(len(resp_body_bytes)))
            for k, v in resp_headers:
                if k.lower() not in ("transfer-encoding", "content-encoding"):
                    self.send_header(k, v)
            self.send_header("X-ToolRecall-Cache", "NOCACHE")
            self.end_headers()
            self.wfile.write(resp_body_bytes)
            return

        # Check cache — only serve cached 2xx responses
        cached = self._client.send(
            {
                "cmd": "cached_api_check",
                "request_hash": request_hash,
            }
        )
        if cached.get("cached"):
            status = cached.get("status", 200)
            # Don't replay non-2xx responses even if cached
            if status < 200 or status >= 300:
                log.warning(
                    "Skipping cached non-2xx response (status %d) for %s %s%s",
                    status,
                    method,
                    target_host,
                    target_path,
                )
            else:
                log.info(
                    "API CACHE HIT: %s %s%s (hash=%s, saved ~%s tokens)",
                    method,
                    target_host,
                    target_path,
                    request_hash[:12],
                    cached.get("tokens_not_read_from_disk", "?"),
                )
                self.send_response(status)
                for hdr_key, hdr_val in cached.get("headers", {}).items():
                    # connection is OUR framing decision — a stored upstream
                    # "Connection: keep-alive" makes HTTP/1.0 clients mis-frame
                    # the replayed body (truncation/hang on socket reuse).
                    if hdr_key.lower() not in (
                        "transfer-encoding",
                        "content-encoding",
                        "connection",
                    ):
                        self.send_header(hdr_key, hdr_val)
                self.send_header("X-ToolRecall-Cache", "HIT")
                cached_body = cached["body"]
                cached_body_bytes = (
                    cached_body.encode("utf-8") if isinstance(cached_body, str) else cached_body
                )
                # Content-Length MUST be set explicitly on HIT replays: the
                # cached header dict may lack it (or carry a stale value), and
                # a close-delimited/keep-alive response with no length makes
                # HTTP/1.1 clients (e.g. the Warp edge relay) block waiting for
                # the declared body until their read timeout fires.
                # MUST count BYTES, not str chars — multibyte UTF-8 in model
                # output makes len(str) < len(utf-8 bytes), which truncates
                # the client-side read and corrupts the replayed JSON.
                self.send_header("Content-Length", str(len(cached_body_bytes)))
                self.end_headers()
                self.wfile.write(cached_body_bytes)
                # Log usage — the cached body still contains the original usage field
                _log_proxy_usage("HIT", target_host, target_path, request_hash, cached_body)
                return

        # Cache MISS — forward to real API
        log.info("API CACHE MISS: %s %s%s — forwarding...", method, target_host, target_path)
        _t_fwd = time.perf_counter()
        resp_status, resp_headers, resp_body = self._forward(
            method,
            target_host,
            target_path,
            target_scheme,
            body_bytes,
        )
        log.info(
            "API FORWARD DONE: %s %s%s status=%d dur=%.0fms bytes=%d",
            method,
            target_host,
            target_path,
            resp_status,
            (time.perf_counter() - _t_fwd) * 1000,
            len(resp_body) if resp_body else 0,
        )

        # Store in cache — only cache 2xx responses
        if 200 <= resp_status < 300:
            # Convert headers list[tuple] to dict for JSON transport
            headers_dict = {}
            for k, v in resp_headers:
                if k.lower() not in headers_dict:
                    headers_dict[k] = v  # first wins (preserves Content-Type etc.)
            # Strip content-encoding — we stripped Accept-Encoding from the
            # outgoing request, so the upstream response is uncompressed.
            headers_dict.pop("Content-Encoding", None)
            headers_dict.pop("content-encoding", None)
            headers_dict.pop("Connection", None)
            headers_dict.pop("connection", None)
            # Body must be str for JSON transport (api_cache schema stores TEXT)
            body_str = (
                resp_body.decode("utf-8", errors="replace")
                if isinstance(resp_body, bytes)
                else resp_body
            )
            self._client.send(
                {
                    "cmd": "cached_api_store",
                    "request_hash": request_hash,
                    "method": method,
                    "host": target_host,
                    "path": target_path,
                    "request_body_hash": body_hash,
                    "response_status": resp_status,
                    "response_headers": headers_dict,
                    "response_body": body_str,
                    "ttl": int(os.environ.get("TOOLRECALL_API_TTL", "300")),
                }
            )

        # Respond with Content-Length (body is flat from .read())
        resp_body_bytes = resp_body if isinstance(resp_body, bytes) else resp_body.encode("utf-8")
        self.send_response(resp_status)
        self.send_header("Content-Length", str(len(resp_body_bytes)))
        for k, v in resp_headers:
            if k.lower() not in ("transfer-encoding", "content-encoding"):
                self.send_header(k, v)
        self.send_header("X-ToolRecall-Cache", "MISS")
        self.end_headers()
        self.wfile.write(resp_body_bytes)

        # Log usage from the live response body
        if 200 <= resp_status < 300:
            _log_proxy_usage(
                "MISS",
                target_host,
                target_path,
                request_hash,
                body_str if isinstance(resp_body, bytes) else resp_body,
            )
        else:
            _log_proxy_usage("MISS", target_host, target_path, request_hash)

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def do_PUT(self):
        self._handle("PUT")

    def do_DELETE(self):
        self._handle("DELETE")

    def do_PATCH(self):
        self._handle("PATCH")

    def _forward(self, method: str, host: str, path: str, scheme: str, body: bytes) -> tuple:
        """Forward request to the real API server.

        Returns (status_code, list_of_headers, body_bytes).

        Resilience (opt-in via [resilience]): retry with jittered backoff on
        connection errors and 429/502/503/504 — ONLY while no response body
        has been consumed (billing/idempotency guard) — and a per-host
        circuit breaker that fast-fails 503 when the upstream is down.
        """
        # Defense-in-depth SSRF guard at the connection sink (py/full-ssrf):
        # never open a connection to a non-allowlisted host even if this
        # method is reached from a path that skipped the _handle gate.
        if not _host_allowed(host):
            log.warning("Blocked forward to non-allowlisted host %r (SSRF guard)", host)
            return (
                403,
                [("Content-Type", "application/json")],
                json.dumps(
                    {
                        "error": "Forbidden: non-allowlisted target host",
                    }
                ).encode(),
            )

        from toolrecall.resilience import CircuitBreaker, CircuitBreakerOpen

        global _forward_breaker
        breaker = None
        if _resilience_flag("circuit_breaker"):
            if _forward_breaker is None:
                _forward_breaker = CircuitBreaker(
                    failure_threshold=int(_resilience_num("cb_failure_threshold", 5)),
                    window=float(_resilience_num("cb_window", 60)),
                    open_seconds=float(_resilience_num("cb_open_seconds", 30)),
                )
            breaker = _forward_breaker
            log.debug("CB active for %s: state=%s", host, breaker.state)

        do_retry = _resilience_flag("retry")
        max_attempts = int(_resilience_num("retry_max_attempts", 3))
        backoff_base = _resilience_num("retry_backoff_base", 0.25)

        def _attempt() -> tuple:
            return self._forward_once(method, host, path, scheme, body)

        class _UpstreamFailure(Exception):
            """Signal for breaker.call(): the attempt ended in a 429/5xx."""

            def __init__(self, status: int, headers: list, resp_body: bytes):
                super().__init__(f"upstream status {status}")
                self.status = status
                self.headers = headers
                self.resp_body = resp_body

        def _guarded() -> tuple:
            try:
                status, headers, resp_body = self._forward_with_retry(
                    _attempt, do_retry, max_attempts, backoff_base
                )
            except _UpstreamFailure:
                raise
            # Outcome-based breaker accounting: 429/5xx responses are
            # failures even though they return normally. Raising inside
            # call() routes them through the breaker's except-path (the
            # success path would clear the failure history).
            if breaker is not None and status in (429, 500, 502, 503, 504):
                raise _UpstreamFailure(status, headers, resp_body)
            return status, headers, resp_body

        try:
            if breaker is not None:
                try:
                    return breaker.call(_guarded)
                except _UpstreamFailure as e:
                    return e.status, e.headers, e.resp_body
            return self._forward_with_retry(_attempt, do_retry, max_attempts, backoff_base)
        except CircuitBreakerOpen as e:
            log.warning(
                "Circuit breaker OPEN for %s — fast-fail (retry after %.0fs)", host, e.retry_after
            )
            return (
                503,
                [("Content-Type", "application/json"), ("Retry-After", str(int(e.retry_after)))],
                json.dumps(
                    {
                        "error": {
                            "code": "circuit_open",
                            "host": host,
                            "retry_after": int(e.retry_after),
                        }
                    }
                ).encode(),
            )

    def _forward_with_retry(
        self, attempt, do_retry: bool, max_attempts: int, backoff_base: float
    ) -> tuple:
        """Run ``attempt`` with optional retry on connection errors / retryable 5xx.

        NEVER retries once a response body has been successfully read
        (the request may have been billed); only transport-level failures
        and pre-body status codes qualify.
        """

        last: tuple | None = None
        for n in range(1, max_attempts + 1 if do_retry else 2):
            try:
                status, headers, resp_body = attempt()
            except Exception:
                if n < (max_attempts if do_retry else 1):
                    if backoff_base > 0:
                        import random
                        import time

                        time.sleep(backoff_base * (2 ** (n - 1)) + random.uniform(0, backoff_base))
                    continue
                raise
            if status in (429, 502, 503, 504) and n < (max_attempts if do_retry else 1):
                # Retry-After honored (bounded by max_attempts anyway)
                last = (status, headers, resp_body)
                if backoff_base > 0:
                    import random
                    import time

                    ra = 0.0
                    for k, v in headers:
                        if k.lower() == "retry-after":
                            try:
                                ra = float(v)
                            except ValueError:
                                ra = 0.0
                    time.sleep(
                        min(ra, 5.0)
                        if ra
                        else backoff_base * (2 ** (n - 1)) + random.uniform(0, backoff_base)
                    )
                continue
            return status, headers, resp_body
        return last  # type: ignore[return-value]

    def _forward_once(self, method: str, host: str, path: str, scheme: str, body: bytes) -> tuple:
        """Single forward attempt: connect, send, read. No retry logic."""
        # SECURITY: Never fall back to plaintext HTTP for known API hosts.
        # Loopback targets (localhost, 127.0.0.1, ::1) always use HTTP since
        # the daemon proxy speaks HTTP on its local port.
        is_loopback = host.split(":")[0] in ("localhost", "127.0.0.1", "::1")
        try:
            if is_loopback:
                conn = http.client.HTTPConnection(host, timeout=_FORWARD_TIMEOUT)
            else:
                conn = http.client.HTTPSConnection(host, timeout=_FORWARD_TIMEOUT)
        except Exception as e:
            log.error("Cannot establish HTTPS connection to %s: %s", host, e)
            return (
                502,
                [("Content-Type", "application/json")],
                json.dumps(
                    {
                        "error": f"HTTPS connection failed: {e}",
                    }
                ),
            )

        # Copy headers, dropping the ones we shouldn't forward
        headers = {}
        skip_headers = {
            "host",
            "connection",
            "proxy-connection",
            "transfer-encoding",
            "content-length",
            "accept-encoding",
        }
        for k, v in self.headers.items():
            if k.lower() not in skip_headers:
                headers[k] = v

        try:
            conn.request(method, path, body=body or None, headers=headers)
            resp = conn.getresponse()
            resp_body = resp.read()
            resp_headers = resp.getheaders()
            conn.close()
            return resp.status, resp_headers, resp_body
        except Exception as e:
            log.error("Forward failed for %s %s%s: %s", method, host, path, e)
            return (
                502,
                [("Content-Type", "application/json")],
                json.dumps(
                    {
                        "error": f"Forward failed: {e}",
                    }
                ),
            )

    def _forward_streaming(
        self,
        method: str,
        host: str,
        path: str,
        scheme: str,
        body: bytes,
        canonical_profile: str | None = None,
        request_hash: str | None = None,
        body_for_tee: bytes | None = None,
    ):
        """Forward request and relay response as chunked/streaming.

        With canonical_profile set (opt-in via X-ToolRecall-Canonicalize),
        the SSE response is additionally TEE'd into a non-stream cache entry
        under request_hash, so future identical requests replay as cache
        HITs (stream-cache, Warp live-test 2026-09-03).

        Without canonicalization, behavior is unchanged: pure chunked relay,
        usage log entry with cache_status=STREAM.
        """
        # SSRF guard — never open a streaming connection to a non-allowlisted host.
        if not _host_allowed(host):
            log.warning("Blocked streaming forward to non-allowlisted host %r (SSRF guard)", host)
            self.send_response(403)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error":"Forbidden: non-allowlisted target host"}')
            return

        import http.client

        is_loopback = host.split(":")[0] in ("localhost", "127.0.0.1", "::1")
        try:
            if is_loopback:
                conn = http.client.HTTPConnection(host, timeout=_FORWARD_STREAM_TIMEOUT)
            else:
                conn = http.client.HTTPSConnection(host, timeout=_FORWARD_STREAM_TIMEOUT)
        except Exception as e:
            log.error(
                "Cannot establish %s connection to %s: %s",
                "HTTP" if is_loopback else "HTTPS",
                host,
                e,
            )
            self.send_response(502)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": f"HTTPS connection failed: {e}"}).encode())
            return

        headers = {}
        skip_headers = {
            "host",
            "connection",
            "proxy-connection",
            "transfer-encoding",
            "content-length",
        }
        for k, v in self.headers.items():
            if k.lower() not in skip_headers:
                headers[k] = v

        try:
            conn.request(method, path, body=body or None, headers=headers)
            resp = conn.getresponse()

            # Relay status line
            self.send_response(resp.status)

            # Relay headers, dropping transfer-encoding (we'll use chunked)
            for k, v in resp.getheaders():
                if k.lower() not in (
                    "transfer-encoding",
                    "content-encoding",
                    "content-length",
                    "connection",
                ):
                    self.send_header(k, v)
            self.send_header("X-ToolRecall-Cache", "STREAM")
            self.send_header("X-ToolRecall-Stream", "passthrough")
            self.end_headers()

            # Relay body chunk by chunk — SSE lines or raw bytes.
            # With canonical_profile: tee chunks for later cache store.
            tee_buf: list[bytes] = []
            while True:
                chunk = resp.read(4096)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
                if canonical_profile:
                    tee_buf.append(chunk)

            conn.close()

            # Tee → reconstruct non-stream response → store under canonical hash
            if canonical_profile and tee_buf and request_hash:
                self._store_stream_tee(
                    tee_buf,
                    request_hash,
                    body_for_tee or b"",
                    host,
                    path,
                )
        except Exception as e:
            log.error("Streaming forward failed for %s %s%s: %s", method, host, path, e)
            try:
                self.wfile.write(b"\n\n[ToolRecall streaming error]\n")
            except Exception:
                pass

        # Log STREAM usage — prompt tokens estimated from request body,
        # completion tokens unavailable (SSE chunks, not parseable here).
        import hashlib

        stream_body_hash = hashlib.sha256(body).hexdigest() if body else ""
        pt_est = max(1, len(body) // 4) if body else 0
        log.info(
            "STREAM: %s %s%s (body=%d bytes, ~%d est prompt tokens)",
            method,
            host,
            path,
            len(body or b""),
            pt_est,
        )
        _log_proxy_usage(
            "STREAM", host, path, request_hash=stream_body_hash, prompt_tokens_override=pt_est
        )

    def _replay_stream_from_cache(
        self, cached: dict, host: str, path: str, request_hash: str | None = None
    ) -> int | None:
        """Serve a cache HIT for a streaming request as RAW SSE passthrough.

        The tee stored the live provider's SSE stream verbatim; replaying it
        byte-for-byte is shape-exact by construction — the client sees
        exactly what a live call delivered (chunk fragmentation, content:null
        deltas, tool_call fragments, provider comments, native_finish_reason).

        Returns prompt tokens saved (from the stored SSE's usage block), or
        None if the cached body isn't replayable (caller falls back to live).
        Never raises.
        """
        try:
            raw = cached.get("body", "")
            if not isinstance(raw, str) or "data:" not in raw:
                return None
            if "[DONE]" not in raw:
                log.warning("STREAM REPLAY: refusing truncated cached stream (no [DONE])")
                return None
            # usage extraction for the usage log
            prompt_tokens = 0
            for line in raw.splitlines():
                line = line.strip()
                if line.startswith("data: ") and line != "data: [DONE]":
                    try:
                        obj = json.loads(line[6:])
                        u = obj.get("usage")
                        if u and u.get("prompt_tokens"):
                            prompt_tokens = u["prompt_tokens"]
                    except json.JSONDecodeError:
                        continue
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-ToolRecall-Cache", "HIT")
            self.send_header("X-ToolRecall-Stream", "replay")
            self.end_headers()
            self.wfile.write(raw.encode("utf-8") if isinstance(raw, str) else raw)
            self.wfile.flush()
            return prompt_tokens
        except Exception:
            return None

    def _store_stream_tee(
        self,
        tee_buf: list,
        request_hash: str,
        request_body: bytes,
        host: str,
        path: str,
    ) -> None:
        """Reconstruct a non-stream response from SSE chunks and store it.

        Best-effort: any parse/store failure is logged and swallowed — the
        client already has its streamed response.
        """
        try:
            raw = b"".join(tee_buf).decode("utf-8", "replace")
            content_parts: list = []
            tool_calls_acc: list = []
            usage = {}
            finish_reason = None
            resp_id = None
            model = None
            for line in raw.splitlines():
                line = line.strip()
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    break
                try:
                    obj = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                resp_id = obj.get("id") or resp_id
                model = obj.get("model") or model
                u = obj.get("usage")
                if u:
                    usage = u  # noqa: F841  (kept for parity with non-stream path)
                for ch in obj.get("choices") or []:
                    delta = ch.get("delta") or {}
                    if delta.get("content"):
                        content_parts.append(delta["content"])
                    if delta.get("tool_calls"):
                        # Streaming tool_calls arrive as index-keyed fragments
                        # (id/function.name in the first, function.arguments
                        # concatenated across deltas). Accumulate them — a
                        # tool-call turn is the norm for agent harnesses.
                        for tc in delta["tool_calls"]:
                            idx = tc.get("index", 0)
                            while len(tool_calls_acc) <= idx:
                                tool_calls_acc.append(
                                    {
                                        "id": "",
                                        "type": "function",
                                        "function": {"name": "", "arguments": ""},
                                    }
                                )
                            acc = tool_calls_acc[idx]
                            if tc.get("id"):
                                acc["id"] = tc["id"]
                            if tc.get("type"):
                                acc["type"] = tc["type"]
                            fn = tc.get("function") or {}
                            if fn.get("name"):
                                acc["function"]["name"] = fn["name"]
                            if fn.get("arguments"):
                                acc["function"]["arguments"] += fn["arguments"]
                    if ch.get("finish_reason"):
                        finish_reason = ch["finish_reason"]  # noqa: F841 (stream summary)

            # Store the RAW SSE stream verbatim. Replaying the captured live
            # stream byte-for-byte is shape-exact by construction — a
            # synthesized chunk structure (measured live, Warp sessions
            # 2026-09-03/04) risks dropping fields the harness client's
            # assembler expects (role-anchoring, content:null deltas,
            # tool_call fragments). The stored body IS the stream.
            if "[DONE]" not in raw:
                log.warning(
                    "STREAM TEE: discarding truncated stream (%d bytes, no [DONE])",
                    len(raw),
                )
                return
            body = raw
            self._client.send(
                {
                    "cmd": "cached_api_store",
                    "request_hash": request_hash,
                    "method": "POST",
                    "host": host,
                    "path": path,
                    "request_body_hash": hashlib.sha256(request_body).hexdigest()
                    if request_body
                    else "",
                    "response_status": 200,
                    "response_headers": {"Content-Type": "application/json"},
                    "response_body": body,
                    "ttl": int(os.environ.get("TOOLRECALL_API_TTL", "300")),
                }
            )
            log.info(
                "STREAM TEE: stored %d chars under canonical hash %s…",
                len("".join(content_parts)),
                request_hash[:12],
            )
        except Exception as e:
            log.warning("Stream tee/store failed (non-fatal): %s", e)

    def log_message(self, format, *args):
        log.debug("ForwardProxy: " + format, *args)


class ThreadedHTTPServer(ThreadingMixIn, http.server.HTTPServer):
    """Threaded HTTP server — handles requests in parallel threads.
    One streaming request no longer blocks all other proxy traffic.
    """

    allow_reuse_address = True
    daemon_threads = True


def run_forward_proxy(bind: str = "127.0.0.1", port: int | None = None):
    """Start the ToolRecall forward proxy (caching API responses).

    Port priority:
      1. --port CLI argument
      2. TOOLRECALL_FORWARD_PORT env var
      3. 8569 (default)

    Binds to localhost only (safe default). No network exposure.
    On cache hit, returns the cached response directly — no API call, no token cost.
    """
    from toolrecall.logging_setup import setup_logging

    setup_logging()  # payload-free rotating file log for the log.* calls below
    if port is None:
        port = int(os.environ.get("TOOLRECALL_FORWARD_PORT", "8569"))
    try:
        server = ThreadedHTTPServer((bind, port), ForwardProxyHandler)
        actual_port = server.server_port
    except OSError as e:
        import errno

        if e.errno == errno.EADDRINUSE:
            log.error("Port %d already in use", port)
            return
        raise

    log.info("ToolRecall Forward Proxy running on http://%s:%d", bind, actual_port)
    print(f"Forward proxy: http://{bind}:{actual_port}")
    sys.stdout.flush()

    # Check daemon
    client = TransportClient()
    ping = client.send({"cmd": "ping"})
    if ping.get("error") == "daemon_unavailable":
        log.warning("ToolRecall daemon not running! Start with: toolrecall daemon &")
        log.info("Forward proxy started — will cache when daemon becomes available")
    else:
        log.info("Connected to ToolRecall daemon")

    log.info("Forwarding for %d known API hosts:", len(FORWARD_HOSTS))
    for h in sorted(FORWARD_HOSTS):
        log.info("  o %s", h)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Shutting down.")


# ─── Debug/Demo Server ──────────────────────────────
# Minimal HTTP server for quick speed demos and debugging.
# 4 endpoints: /read, /term, /stats, /health
# Not a full bridge — just curl-friendly cache access.


class DebugHandler(http.server.BaseHTTPRequestHandler):
    """Minimal HTTP handler for debugging and demos — 4 endpoints."""

    def __init__(self, *args, **kwargs):
        self._client = TransportClient()
        super().__init__(*args, **kwargs)

    def do_GET(self):
        from urllib.parse import urlparse, parse_qs

        parsed = urlparse(self.path)
        path = parsed.path
        params = parse_qs(parsed.query)
        q = {k: v[0] if v else "" for k, v in params.items()}

        try:
            if path == "/read":
                p = q.get("path", "")
                if not p:
                    result = {"error": "Missing 'path' param"}
                else:
                    result = self._client.send({"cmd": "cached_read", "path": p})

            elif path == "/term":
                c = q.get("cmd", "")
                if not c:
                    result = {"error": "Missing 'cmd' param"}
                else:
                    result = self._client.send(
                        {"cmd": "cached_terminal", "command": c, "cwd": os.getcwd()}
                    )

            elif path == "/stats":
                result = self._client.send({"cmd": "cache_status"})

            elif path == "/health":
                ping = self._client.send({"cmd": "ping"})
                if ping.get("error") == "daemon_unavailable":
                    self.send_response(503)
                    result = {"status": "error", "daemon": "not running"}
                else:
                    self.send_response(200)
                    result = {"status": "ok"}

            else:
                self.send_response(404)
                result = {"error": f"Unknown: {path}"}

            if "error" in result and path != "/health":
                self.send_response(500 if result["error"] != "daemon_unavailable" else 503)

            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(result).encode())

        except Exception as e:
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": str(e)}).encode())

    def log_message(self, format, *args):
        log.debug("DebugServer: " + format, *args)


def run_debug_server(bind: str = "127.0.0.1", port: int = 8570):
    """Start minimal debug/demo server on localhost (:8570).

    Endpoints:
      GET /read?path=X   -> cached_read
      GET /term?cmd=X    -> cached_terminal
      GET /stats         -> cache statistics
      GET /health        -> daemon status
    """
    try:
        server = ThreadedHTTPServer((bind, port), DebugHandler)
        actual_port = server.server_port
    except OSError as e:
        if e.errno == 98:
            log.error("Port %d already in use", port)
            return
        raise

    print(f"ToolRecall Debug Server on http://{bind}:{actual_port}")
    log.info("Endpoints: GET /read?path=  GET /term?cmd=  GET /stats  GET /health")
    sys.stdout.flush()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Shutting down.")
