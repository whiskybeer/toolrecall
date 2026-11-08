# Remote Doc Sources — opt-in, and what each guardrail is for

The knowledge base is **local-first**: it indexes the content that is already on
disk (skills, memories, plan files, scanned directories) and needs no network at
all. On top of that, a user can opt in to *remote* sources:

```toml
[[sources.remote]]
name       = "hermes-docs"
kind       = "llms_full"
url        = "https://hermes-agent.nousresearch.com/docs/llms-full.txt"
base_url   = "https://hermes-agent.nousresearch.com/docs/"
path_strip = "website/"        # optional: drop a repo prefix from bundle paths
max_pages  = 2000
max_bytes  = 33554432
```

```bash
toolrecall index-remote                 # sync every configured source
toolrecall index-remote --source NAME   # one source
toolrecall index-remote --force         # ignore ETag / Last-Modified
toolrecall docs-rejects                 # what was skipped or flagged, and why
```

Once synced, the source is searchable exactly like local content:
`docs_search("<query>", source="hermes-docs")` and
`docs_get_page("<path>", "hermes-docs")`.

## Not a crawler — that is the safety property, not a limitation

Two `kind`s exist, and both are **fixed URL sets a human wrote down**:

| `kind` | What it fetches |
|---|---|
| `llms_full` | one URL holding a whole corpus (`llms-full.txt` convention; pages separated by `<!-- source: <path> -->` markers) |
| `urls` | an explicit list of page URLs from your config |

Nothing follows links. Nothing reads a sitemap. Nothing takes a depth. A source
cannot grow beyond what you typed, so "the crawl went somewhere I did not
expect" is not a failure mode this tool can have.

## Every guardrail, and the failure it answers

| Guardrail | Failure it answers |
|---|---|
| byte cap per source (`max_bytes`, default 32 MB) | a script-generated bundle is half a gigabyte and blows up RAM/disk |
| page cap (`max_pages`, default 2000) | same, from the other direction |
| `robots.txt` honoured for `kind="urls"`; **unreadable robots.txt = refusal** | "we could not ask, so we assumed yes" is how a tool ends up somewhere it was told not to go. `404` (no rules published) is the one absence that counts as a yes |
| one request at a time, one second apart, one process | the 2026-09-28 incident: a docs-server pool spawned 4+ concurrent `uv` processes and drove host load to 79 on 2 vCPU |
| fetch → parse → **validate** → replace, in one transaction | a truncated download silently replacing pages that were already good |
| `If-None-Match` / `If-Modified-Since` | re-downloading an unchanged corpus every day |
| provenance header on `docs_get_page`, footnote on `docs_search` | fetched text reaching a model with nothing marking it as data |
| injection-*flagging* (never dropping) | real documentation talks *about* prompts and instructions; dropping on a pattern match would delete true pages |

## Rejections are recorded, because an opt-in must be falsifiable

Every skip and every flag is written to a ledger with a count, per
`(source, path, reason)`, and accumulates across runs. `toolrecall docs-rejects`
prints the most-rejected pages first:

```
Rejected / flagged pages
  kind      count  source         reason               path
  rejected      3  hermes-docs    robots               https://…/private/x  (last …)
  flagged       1  hermes-docs    injection_suspect    docs/hostile         (last …)

  rejected = not indexed  ·  flagged = indexed, served with a warning
```

The distinction matters: **rejected** means the page is not in the knowledge
base; **flagged** means it is, but every read of it carries a warning. Neither
hides behind the other.

An empty table prints that it is *not* proof of health — it accumulates only
from syncs that actually ran, so "nothing recorded" and "nothing looked" must
not read the same.

## What this is not

- **It is not a security boundary.** A source you opt into is a source you have
  decided to trust. The guardrails limit *how much* text arrives, from *where*,
  and mark it — they do not make the text trustworthy.
- **It is not an injection defence.** Flagging is a narrow pattern scan over text
  addressed at an agent; it has no false-negative guarantee, and it cannot make a
  model treat fetched text as data. If that residual risk is unacceptable for
  your use case, do not opt in: local content needs no disclaimer.
- **It is not a substitute for the crawler that had a second database.** The
  `docs_mcp.db`/`docs_server.py` pair in a user workspace is the local
  workaround this replaces: one knowledge base, one server implementation, no
  workspace scripts.
