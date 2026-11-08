# MCP Cache Serving Policy

How ToolRecall decides whether a cached MCP tool result may be delivered.

**Default for shared/multi-agent caches: validate before serve (S3).**
Clock-based freshness (TTL, stale-while-revalidate) is a *performance*
mechanism, not a *correctness* mechanism. Use it only for single-agent,
single-session caches where a wrong result costs a retry rather than a
wrong answer.

---

## The problem

An HTTP cache can serve a stale page and the user notices nothing worse than
an outdated timestamp. A tool-result cache feeds an autonomous agent, which
acts on the result. A stale `read_file` makes it patch code that no longer
exists; a stale error makes it abandon a working capability.

Tool results also have no `Last-Modified` semantics an agent can reason
about: the same request against the same server can legitimately return
different bytes seconds apart. The only question that matters is not
"is this entry young?" but **"is this entry still equal to what the origin
would return right now?"**

## The measurement

Phase C (2026-09-20) replayed 240 shared requests from 3 agents against a
controllable origin with a scripted mutation schedule (content change, error
injection, heal, byte-level rollback). Ground truth was read from the origin
at the serve instant, never through the cache.

| Policy | hit % | **wrong %** | status flips | origin executions |
|---|---|---|---|---|
| no cache | 0.0 | **0.00** | 0 | 240 |
| TTL + SWR, no validator | 96.7 | **29.17** | 28 | 16 |
| TTL + revalidate **at expiry** | 96.2 | **28.33** | 28 | 9 |
| **validate before every serve** | 82.5 | **0.00** | **0** | 42 |
| TTL + revalidate at expiry + SWR | 96.7 | **28.75** | 28 | 9 |
| history-prefix match (no validator) | 88.3 | **36.67** | 28 | 28 |

Three results drive the default:

1. **A validator you don't consult is not a validator.** The three
   clock-based policies land within 0.8 pp of each other (29.17 / 28.33 /
   28.75) even though two of them *store* ETags. Validating only at TTL
   expiry means that inside the TTL window the cache is behaviourally
   identical to having no validator at all: 231 hits were delivered against
   8 validator checks.
2. **Request identity is not answer identity.** Matching the full call
   history (the strongest identity-based scheme) scored *worst* — 36.67%.
   Identity-based schemes answer "same request?"; a shared cache must answer
   "same answer?".
3. **Correctness is affordable.** Validate-before-serve cost ~14 pp of hit
   rate, but 198 of its 232 validations were answered `304 Not Modified` —
   header-only, no body, no recompute. The origin executed 42 of 240
   requests: still a **5.7× execution reduction, with zero wrong
   deliveries.**

## The policy

```
on request:
    entry = cached_mcp_check(server, tool, args)
    if not entry["cached"]:
        result = <call origin>
        cached_mcp_store(..., data=result.body, etag=result.etag)
        return result

    # entry exists — do NOT serve it yet
    fresh = <conditional request to origin, If-None-Match: entry["etag"]>
    if fresh.status == 304:
        return entry["data"]          # validated, safe to serve
    cached_mcp_store(..., data=fresh.body, etag=fresh.etag)
    return fresh.body
```

`cached_mcp_check` returns `etag` on every hit (fresh and stale) so the
caller can build the conditional request. `cached_mcp_store` accepts
`etag=` to make the next validation possible. Both are reachable over the
daemon IPC surface (`cached_mcp_check` / `cached_mcp_store` with an `etag`
field), so gateways and multiplexers can implement this policy without
importing ToolRecall in-process.

## When TTL-only is acceptable

- Single-agent, single-session caches where the agent itself caused any
  change it would be served back (the stock local daemon case).
- Tools whose results are genuinely immutable for the TTL window
  (content-addressed reads, pinned versions, frozen datasets).
- Environments frozen for the duration of a run (RL rollouts, replay
  harnesses) — this is why history-prefix caching works in that literature
  and does not transfer to live shared caches.

In every other shared configuration, prefer validate-before-serve.

## What this does not protect against

- **A compromised or lying origin.** Validators prove "the origin still
  returns these bytes", never "these bytes are correct". See SECURITY.md §8.
- **Tools without a validator.** If a server returns no ETag, a stored row
  cannot be validated. Such tools must degrade *loudly* — serve uncached, or
  refuse to share the entry — never silently fall back to clock-based
  freshness. Measure the no-validator fraction before deploying a shared
  cache.
- **Timestamp validators on rollback.** The Phase C schedule includes content
  returning to an earlier byte state. Hash-based ETags handle this correctly;
  a `Last-Modified` validator mis-fires.
- **Auth-scoped results.** A shared entry must bind the auth context into its
  key, or one caller's permissions leak into another's answer.

## Reproducing

The measurement harness lives outside the repo (bench workspace):
`phase_c.py --all` runs all six policies against the controllable origin and
prints the table above. Results are deterministic and require no provider
calls.
