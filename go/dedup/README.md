# go-dedup — request-level dedup, ported to Go

A pure, **dependency-free** Go port of ToolRecall's `dedup_messages()`
(`toolrecall/adapters/litellm.py`) — the request-level duplicate-content
dedup that stubs repeated large text blocks in agent tool loops.

**Why a Go port exists:** the reference implementation is a LiteLLM plugin
(Python). Products that are Go / no-runtime can't take the
Python artifact, but the underlying logic is ~70 lines of pure, no-dependency
dedup. This package is that logic, vendorable under your own review by copying
a single `dedup.go` file.

## What it does

Scans a chat-completion `messages` array for large text blocks that appear
more than once in the same request (typically repeated file reads / tool
results in agent loops) and replaces every duplicate *after* the first
occurrence with a short stub.

- **Keep-first, stub-later** — earlier bytes never change when a later duplicate
  appears, so the provider's prefix cache keeps hitting.
- **Deterministic** — same input + same options → same output.
- **Whole-block exact matching** — a file embedded inside a longer string is a
  different block and will not match (identical to the Python original).
- **Stdlib only** — vendorable by copying `dedup.go` (plus `go.mod` if you want
  the module).

## Usage

```go
import "github.com/whiskybeer/toolrecall/go-dedup"

opts := dedup.DefaultOptions()
opts.ProtectLast = 0 // append-invariant / fully prefix-stable (the pilot mode)
opts.MinChars = 800

newMessages, stats := dedup.DeduplicateMessages(agentConversation, opts)
// stats.Blocks, stats.CharsSaved, stats.EstTokensSaved
```

Messages are modeled as `map[string]interface{}` so the port accepts any
OpenAI-compatible schema — string `content` **and** the content-parts list
form (`[{type, text}, ...]`). Input is never mutated; a fresh slice and
shallow-copied maps are returned.

## Options

| Field | Default | Meaning |
|-------|---------|---------|
| `MinChars` | 800 | Only dedup blocks at least this large (bytes) |
| `ProtectLast` | 2 | Never stub inside the last N messages. `0` = prefix-stable |
| `StubRoles` | `[tool, function, user]` | Roles whose content may be stubbed. System is registered but never modified |

## Behavior parity

`go test ./...` mirrors the Python self-test (same fixture, same assertions)
and passes. A parity command (`go run ./parity`) prints the stub strings for
the canonical fixture — **byte-identical** to the Python output, including the
`sha256[:16]` digests and `message N` references.

### Documented differences from Python

| Aspect | Python | Go |
|--------|--------|-----|
| `len(text)` | code points | bytes (Go strings are UTF-8) — for ASCII-dominated file/tool content the heuristics agree; multibyte-heavy content counts slightly higher |
| charset encoding | `utf-8, surrogatepass` | Go strings are always valid UTF-8; lone surrogates cannot appear |

Both are deliberate and don't affect ASCII-dominated agent traffic.

## Known limitations (inherited from the Python original)

- **LiteLLM `/v1/messages` bypass** — LiteLLM skips the pre-call hook on the
  Anthropic-format endpoint (BerriAI/litellm#27518). Route requests through
  the OpenAI-format `/v1/chat/completions` endpoint for the hook to fire.
- **Volume-stubbable ≠ billed-$ savings** — this reports chars/tokens of
  *duplicate volume* removed, not billed dollars. On providers with cheap
  cache-reads, a cached input token costs a fraction of an uncached one; report
  `cache_read_tokens` alongside to be honest about net cost.

## Build & test

```bash
cd go/dedup
go vet ./...
go test -v ./...          # 6 tests, parity with Python self-test
go run ./parity           # prints stub strings for manual diff vs python
```

## Relationship to the daemon

This is **standalone request-level dedup**, not ToolRecall's drop-and-recall
context tracker. A gateway never sees the agent's tool loop, so there is no
recall to serve — it composes with (and does **not** require) the ToolRecall
daemon.