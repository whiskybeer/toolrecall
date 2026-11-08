"""Remote documentation sources for the knowledge base — generic and opt-in.

Local-first by design: the shipped configuration contains no remote source, and
the in-core knowledge base keeps serving the local content (skills, memories,
plans, scanned dirs) either way. A user opts in per source:

    [[sources.remote]]
    name       = "hermes-docs"
    kind       = "llms_full"
    url        = "https://hermes-agent.nousresearch.com/docs/llms-full.txt"
    base_url   = "https://hermes-agent.nousresearch.com/docs/"
    path_strip = "website/"     # optional: drop a repo prefix from bundle paths
    max_pages  = 2000
    max_bytes  = 33554432

Two kinds exist, and BOTH are fixed URL sets a human wrote down:

  llms_full   one URL holding a whole corpus (the ``llms-full.txt`` convention,
              pages separated by ``<!-- source: <path> -->`` markers)
  urls        an explicit list of page URLs in the config

This is not a crawler: nothing follows links, nothing reads a sitemap, nothing
takes a depth. That is the safety property, not a missing feature.

Guardrails, each one answering a concrete failure mode:

* byte cap per source (a script-generated bundle can be half a gigabyte) and a
  page cap (same reason); both are RECORDED when they bite, never silent
* ``robots.txt`` is honoured for ``kind="urls"``; a disallowed — or unreachable —
  robots.txt means the page/source is skipped and recorded, never fetched anyway
* one request at a time, one second apart, one process: the 2026-09-28 incident
  where a docs-server pool spawned 4+ concurrent ``uv`` processes and drove host
  load to 79 on 2 vCPUs
* fetch -> parse -> VALIDATE -> replace, in one transaction: a truncated download
  must never delete pages that are already good
* external content is labelled on the way out (``docs_get_page``,
  ``docs_search``), because text that reaches a model can be read as
  instructions

Every rejection and every suspicion is recorded per ``(source, path, reason)``
and accumulates across runs. Without that, an opt-in is unfalsifiable: you cannot
tell "nothing was rejected" from "we never looked". :func:`rejection_stats`
answers "which files do we reject most, and why" — ``toolrecall docs-rejects``
prints it.
"""

from __future__ import annotations

import re
import sqlite3
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit

from toolrecall import docs as _docs

__all__ = [
    "REJECT_REASONS",
    "FLAG_REASONS",
    "configured_sources",
    "sync_source",
    "sync_all",
    "rejection_stats",
    "format_rejection_stats",
    "page_provenance",
    "ensure_source_tables",
]

# Reasons a page or a whole source is not indexed. Kept as a closed vocabulary so
# the statistics stay comparable across runs.
REJECT_REASONS = (
    "robots",  # robots.txt forbids this page
    "robots_unreachable",  # robots.txt could not be read -> refuse, do not guess
    "too_large",  # over the byte cap
    "too_many_pages",  # over the page cap
    "fetch_error",  # network/HTTP failure
    "parse_error",  # marker format did not yield pages
    "empty",  # download or parse produced nothing usable
    "invalid",  # config entry is not usable (missing name/url/kind)
)

# Reasons a page is indexed but marked. Flag, never drop: real documentation
# legitimately talks ABOUT prompts and instructions, so dropping on a pattern
# match would delete true content. The page is served with a warning instead.
FLAG_REASONS = ("injection_suspect",)

USER_AGENT = "toolrecall-docs/0.1 (+https://toolrecall.dev)"
DEFAULT_MAX_BYTES = 32 * 1024 * 1024
DEFAULT_MAX_PAGES = 2000
DEFAULT_TIMEOUT = 30
RATE_LIMIT_S = 1.0

_MARKER_RE = re.compile(r"<!--\s*source:\s*(.*?)\s*-->")
_HEADING_RE = re.compile(r"^#\s+(.*)$", re.MULTILINE)

# Conservative, deliberately narrow: these match text that is addressed AT an
# agent rather than text discussing agents. A false positive only adds a warning
# line; it never removes content.
_INJECTION_PATTERNS = (
    re.compile(r"ignore\s+(?:all\s+|any\s+)?(?:previous|prior|above)\s+instructions", re.I),
    re.compile(r"disregard\s+(?:all\s+|any\s+)?(?:previous|prior|above)\s+", re.I),
    re.compile(r"do\s+not\s+(?:tell|inform|mention\s+this\s+to)\s+the\s+user", re.I),
    re.compile(r"reveal\s+(?:your\s+)?(?:system\s+prompt|instructions)", re.I),
    re.compile(r"you\s+are\s+now\s+(?:a|an|in)\s+", re.I),
    re.compile(r"new\s+instructions?\s*:", re.I),
)

_STATE_SCHEMA = """
CREATE TABLE IF NOT EXISTS source_state (
    source        TEXT PRIMARY KEY,
    kind          TEXT,
    url           TEXT,
    etag          TEXT,
    last_modified TEXT,
    fetched_at    REAL,
    pages         INTEGER DEFAULT 0,
    rejected      INTEGER DEFAULT 0,
    flagged       INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS source_rejects (
    source  TEXT NOT NULL,
    path    TEXT NOT NULL,
    reason  TEXT NOT NULL,
    count   INTEGER NOT NULL DEFAULT 0,
    last_at REAL,
    detail  TEXT DEFAULT '',
    PRIMARY KEY (source, path, reason)
);
CREATE TABLE IF NOT EXISTS page_meta (
    source      TEXT NOT NULL,
    path        TEXT NOT NULL,
    fetched_at  REAL,
    flagged     INTEGER DEFAULT 0,
    flag_reason TEXT DEFAULT '',
    PRIMARY KEY (source, path)
);
"""


# ── Config ───────────────────────────────────────────────────────────────────


def configured_sources() -> list[dict]:
    """Remote sources from ``[[sources.remote]]``, normalised.

    Invalid entries are returned with ``reason="invalid"`` instead of raising:
    a typo in a config file must not make the tool unusable, and it must be
    visible rather than skipped in silence.
    """
    try:
        cfg = _docs._get_config()
        raw = cfg.get("sources", "remote", default=[])
    except Exception:
        return []
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    for entry in raw:
        if not isinstance(entry, dict):
            out.append({"name": "", "reason": "invalid", "detail": "not a table"})
            continue
        name = str(entry.get("name") or "").strip()
        kind = str(entry.get("kind") or "").strip()
        url = str(entry.get("url") or "").strip()
        src = {
            "name": name,
            "kind": kind,
            "url": url,
            "base_url": str(entry.get("base_url") or "").strip(),
            "path_strip": str(entry.get("path_strip") or ""),
            "urls": [str(u) for u in (entry.get("urls") or [])],
            "max_pages": int(entry.get("max_pages") or DEFAULT_MAX_PAGES),
            "max_bytes": int(entry.get("max_bytes") or DEFAULT_MAX_BYTES),
            "timeout": int(entry.get("timeout") or DEFAULT_TIMEOUT),
            "robots": bool(entry.get("robots", True)),
        }
        if not name or kind not in ("llms_full", "urls"):
            src["reason"] = "invalid"
            src["detail"] = f"name={name!r} kind={kind!r}"
        elif kind == "llms_full" and not url:
            src["reason"] = "invalid"
            src["detail"] = "llms_full needs url"
        elif kind == "urls" and not src["urls"]:
            src["reason"] = "invalid"
            src["detail"] = "urls needs a non-empty urls list"
        out.append(src)
    return out


# ── Storage ──────────────────────────────────────────────────────────────────


def ensure_source_tables(conn: sqlite3.Connection) -> None:
    """Create the ledger tables if missing. Idempotent, no FTS interaction."""
    conn.executescript(_STATE_SCHEMA)


def _connect() -> sqlite3.Connection:
    """Ledger connection in autocommit mode.

    isolation_level=None is deliberate: the rejection ledger is written while a
    sync is still crawling (or after an early refusal), and an implicit
    transaction would hold a write lock for the whole network loop — the daemon
    reads the same DB. It also keeps the explicit ``BEGIN IMMEDIATE`` for the
    page replacement legal: with Python's default isolation level the ledger
    INSERTs leave a transaction open and that BEGIN raises
    "cannot start a transaction within a transaction".
    """
    conn = sqlite3.connect(_docs._get_db_path(), timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.isolation_level = None
    ensure_source_tables(conn)
    return conn


def _record_rejection(conn, source: str, path: str, reason: str, detail: str = "") -> None:
    """Accumulate one rejection. Counts make 'most rejected' answerable."""
    conn.execute(
        "INSERT INTO source_rejects (source, path, reason, count, last_at, detail)"
        " VALUES (?, ?, ?, 1, ?, ?)"
        " ON CONFLICT(source, path, reason) DO UPDATE SET"
        "   count = count + 1, last_at = excluded.last_at,"
        "   detail = CASE WHEN excluded.detail != '' THEN excluded.detail ELSE detail END",
        (source, path, reason, time.time(), detail),
    )


def _save_state(conn, src: dict, **fields) -> None:
    cols = [
        "source",
        "kind",
        "url",
        "etag",
        "last_modified",
        "fetched_at",
        "pages",
        "rejected",
        "flagged",
    ]
    row = dict(
        source=src["name"],
        kind=src.get("kind", ""),
        url=src.get("url", ""),
        etag=None,
        last_modified=None,
        fetched_at=None,
        pages=0,
        rejected=0,
        flagged=0,
    )
    row.update(fields)
    conn.execute(
        f"INSERT INTO source_state ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})"
        f" ON CONFLICT(source) DO UPDATE SET " + ", ".join(f"{c} = excluded.{c}" for c in cols[1:]),
        tuple(row[c] for c in cols),
    )


def _state(conn, source: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM source_state WHERE source = ?", (source,)).fetchone()


# ── Fetching ─────────────────────────────────────────────────────────────────


def _fetch(
    url: str, *, etag=None, last_modified=None, max_bytes=DEFAULT_MAX_BYTES, timeout=DEFAULT_TIMEOUT
) -> dict:
    """One GET, capped. Returns {status, body, truncated, etag, last_modified}."""
    headers = {"User-Agent": USER_AGENT}
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - fixed config URL
        raw = resp.read(max_bytes + 1)
        return {
            "status": resp.status,
            "body": raw[:max_bytes],
            "truncated": len(raw) > max_bytes,
            "etag": resp.headers.get("ETag"),
            "last_modified": resp.headers.get("Last-Modified"),
        }


_robots_cache: dict[str, tuple[bool, str]] = {}


def _robots_allows(url: str, timeout: int) -> tuple[bool, str]:
    """(allowed, reason). Refuses when robots.txt cannot be read — never guesses.

    A documented site may legitimately allow everything, but a site we cannot
    ask is a site we do not touch: an unknown answer is not a yes.
    """
    try:
        parsed = urlsplit(url)
    except Exception:
        parsed = None
    if parsed is None:
        return False, "unparseable URL"
    origin = f"{parsed.scheme}://{parsed.netloc}"
    if origin in _robots_cache:
        allowed, reason = _robots_cache[origin]
        return (allowed if allowed else False), reason
    robots_url = origin + "/robots.txt"
    try:
        req = urllib.request.Request(robots_url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - origin of a configured URL
            text = resp.read(256 * 1024).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        # 404 means "no rules published" — the one case where absence is a yes.
        if e.code == 404:
            _robots_cache[origin] = (True, "no robots.txt")
            return True, "no robots.txt"
        _robots_cache[origin] = (False, f"robots.txt HTTP {e.code}")
        return False, f"robots.txt HTTP {e.code}"
    except Exception as e:
        _robots_cache[origin] = (False, f"robots.txt unreadable: {e}")
        return False, f"robots.txt unreadable: {e}"

    # Only the `User-Agent: *` group applies to us; named groups are ignored.
    path = parsed.path or "/"
    applies, disallowed = False, []
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        field, _, value = line.partition(":")
        field, value = field.strip().lower(), value.strip()
        if field == "user-agent":
            applies = value == "*"
        elif field == "disallow" and applies and value:
            disallowed.append(value)
    for rule in disallowed:
        if path.startswith(rule):
            _robots_cache[origin] = (False, f"robots.txt disallows {rule}")
            return False, f"robots.txt disallows {rule}"
    _robots_cache[origin] = (True, "allowed")
    return True, "allowed"


def _scan_injection(text: str) -> str | None:
    """Return the first pattern that matches text addressed at an agent."""
    for pat in _INJECTION_PATTERNS:
        if pat.search(text):
            return pat.pattern
    return None


# ── Parsing ──────────────────────────────────────────────────────────────────


def _clean_path(raw: str, strip: str) -> str:
    path = (raw or "").strip()
    if strip and path.startswith(strip):
        path = path[len(strip) :]
    for ext in (".mdx", ".md", ".html"):
        if path.endswith(ext):
            path = path[: -len(ext)]
            break
    return path.lstrip("/") or "/"


def _parse_llms_full(text: str, src: dict) -> tuple[list[dict], list[tuple[str, str]]]:
    """Split a bundle into pages. Returns (pages, rejections).

    The format is the one the convention actually ships (verified against a live
    bundle, not assumed): pages are separated by ``<!-- source: <path> -->``
    markers, each followed by a ``# Title`` heading.
    """
    parts = _MARKER_RE.split(text)
    pages: list[dict] = []
    rejects: list[tuple[str, str]] = []
    if len(parts) < 3:  # no markers at all -> not a bundle we can trust
        return [], [("", "parse_error")]
    strip = src.get("path_strip", "")
    base = src.get("base_url", "")
    for i in range(1, len(parts), 2):
        if i + 1 >= len(parts):
            break
        path = _clean_path(parts[i], strip)
        body = parts[i + 1].strip()
        if not body:
            rejects.append((path, "empty"))
            continue
        m = _HEADING_RE.search(body)
        title = m.group(1).strip() if m else (path.rsplit("/", 1)[-1] or path)
        # Bundles often repeat the heading right after the marker ("# X\n\n# X").
        content = body
        if m:
            rest = body[m.end() :].lstrip()
            if rest.startswith("# " + title):
                content = rest
        pages.append(
            {
                "path": path,
                "title": title,
                "content": content,
                "url": (base.rstrip("/") + "/" + path.lstrip("/")) if base else "",
            }
        )
    return pages, rejects


def _parse_urls(src: dict) -> list[dict]:
    pages = []
    for url in src["urls"]:
        path = url.split("://", 1)[-1].split("/", 1)[-1] or "/"
        pages.append({"path": path, "title": path.rsplit("/", 1)[-1], "content": "", "url": url})
    return pages


# ── Sync ─────────────────────────────────────────────────────────────────────


def sync_source(src: dict, force: bool = False, verbose: bool = True) -> dict:
    """Ingest one configured source. Never raises; returns a report dict.

    The replacement is atomic and validated: pages are only swapped in after the
    download parsed into something non-empty. An interrupted or truncated fetch
    therefore leaves the previously good pages untouched.
    """
    name = src.get("name") or "(unnamed)"
    report = {
        "source": name,
        "kind": src.get("kind", ""),
        "unchanged": False,
        "indexed": 0,
        "flagged": 0,
        "rejected": {},
        "errors": [],
    }

    def say(msg: str) -> None:
        if verbose:
            print(msg)

    def reject(path: str, reason: str, detail: str = "") -> None:
        """Record one skip — ledger, report and CLI summary stay in step.

        Every early return path goes through here, so a refusal can never be
        logged to the DB while the report claims nothing was rejected (that
        mismatch is exactly how a guardrail becomes invisible).
        """
        report["rejected"][reason] = report["rejected"].get(reason, 0) + 1
        _record_rejection(conn, name, path, reason, detail)

    if src.get("reason") == "invalid":
        report["errors"].append(f"invalid config entry: {src.get('detail', '')}")
        return report

    conn = _connect()
    try:
        state = _state(conn, name)
        etag = None if force else (state["etag"] if state else None)
        last_mod = None if force else (state["last_modified"] if state else None)

        # ── Fetch ──
        if src["kind"] == "llms_full":
            try:
                got = _fetch(
                    src["url"],
                    etag=etag,
                    last_modified=last_mod,
                    max_bytes=src["max_bytes"],
                    timeout=src["timeout"],
                )
            except Exception as e:
                report["errors"].append(f"fetch failed: {e}")
                reject(src.get("url", ""), "fetch_error", str(e))
                conn.commit()
                return report
            if got["status"] == 304:
                report["unchanged"] = True
                say(f"  {name}: HTTP 304 — already up to date")
                _save_state(
                    conn,
                    src,
                    etag=etag,
                    last_modified=last_mod,
                    fetched_at=time.time(),
                    pages=state["pages"] if state else 0,
                    rejected=state["rejected"] if state else 0,
                    flagged=state["flagged"] if state else 0,
                )
                conn.commit()
                return report
            if got["truncated"]:
                report["errors"].append(
                    f"source exceeds max_bytes ({src['max_bytes']}); refusing to replace"
                )
                reject(src.get("url", ""), "too_large", f">{src['max_bytes']} bytes")
                conn.commit()
                say(f"  {name}: REJECTED — larger than max_bytes, old pages kept")
                return report
            text = got["body"].decode("utf-8", "replace")
            pages, parse_rejects = _parse_llms_full(text, src)
            for path, reason in parse_rejects:
                reject(path, reason)
            new_etag, new_lm = got["etag"], got["last_modified"]
        else:  # kind == "urls"
            pages = []
            for url in src["urls"]:
                if src.get("robots"):
                    allowed, why = _robots_allows(url, src["timeout"])
                    if not allowed:
                        reason = (
                            "robots"
                            if why.startswith("robots.txt disallows")
                            else "robots_unreachable"
                        )
                        reject(url, reason, why)
                        say(f"  {url}: SKIPPED ({why})")
                        continue
                try:
                    got = _fetch(url, max_bytes=src["max_bytes"], timeout=src["timeout"])
                    if got["truncated"]:
                        reject(url, "too_large", f">{src['max_bytes']} bytes")
                        say(f"  {url}: SKIPPED (larger than max_bytes)")
                        continue
                    body = got["body"].decode("utf-8", "replace")
                    m = _HEADING_RE.search(body)
                    pages.append(
                        {
                            "path": _clean_path(url.split("://", 1)[-1], ""),
                            "title": m.group(1).strip() if m else url,
                            "content": body,
                            "url": url,
                        }
                    )
                except Exception as e:
                    reject(url, "fetch_error", str(e))
                    say(f"  {url}: SKIPPED (fetch failed: {e})")
                time.sleep(RATE_LIMIT_S)  # one request at a time, politely spaced
            new_etag = new_lm = None

        # ── Validate before touching the good pages ──
        if not pages:
            report["errors"].append("no pages parsed — keeping existing pages")
            # Only add an aggregate reason when nothing else explains the
            # failure: per-page reasons are already in the ledger, and a phantom
            # row for the source URL would distort "most rejected".
            if report["rejected"]:
                reason = next(iter(report["rejected"]))
            else:
                reason = "empty"
                reject(src.get("url", ""), "empty")
            conn.commit()
            say(f"  {name}: REJECTED — nothing usable ({reason}), old pages kept")
            return report

        if len(pages) > src["max_pages"]:
            dropped = pages[src["max_pages"] :]
            pages = pages[: src["max_pages"]]
            for p in dropped:
                reject(p["path"], "too_many_pages", f"cap {src['max_pages']}")
            say(f"  {name}: page cap {src['max_pages']} — {len(dropped)} pages not indexed")

        # ── Replace, atomically ──
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM pages WHERE source = ?", (name,))
        conn.execute("DELETE FROM page_meta WHERE source = ?", (name,))
        flagged = 0
        now = time.time()
        for p in pages:
            conn.execute(
                "INSERT OR REPLACE INTO pages (source, path, title, content, url)"
                " VALUES (?, ?, ?, ?, ?)",
                (name, p["path"], p["title"], p["content"], p["url"]),
            )
            hit = _scan_injection(p["content"])
            if hit:
                flagged += 1
                conn.execute(
                    "INSERT OR REPLACE INTO page_meta (source, path, fetched_at, flagged, flag_reason)"
                    " VALUES (?, ?, ?, 1, ?)",
                    (name, p["path"], now, hit),
                )
                _record_rejection(conn, name, p["path"], "injection_suspect", hit)
            else:
                conn.execute(
                    "INSERT OR REPLACE INTO page_meta (source, path, fetched_at, flagged, flag_reason)"
                    " VALUES (?, ?, ?, 0, '')",
                    (name, p["path"], now),
                )
        _save_state(
            conn,
            src,
            etag=new_etag,
            last_modified=new_lm,
            fetched_at=now,
            pages=len(pages),
            rejected=sum(report["rejected"].values()),
            flagged=flagged,
        )
        conn.commit()

        report["indexed"] = len(pages)
        report["flagged"] = flagged
        skipped = sum(report["rejected"].values())
        say(
            f"  {name}: {len(pages)} pages indexed"
            + (f", {flagged} flagged" if flagged else "")
            + (f", {skipped} rejected" if skipped else "")
        )
        return report
    finally:
        conn.close()


def sync_all(only: str | None = None, force: bool = False, verbose: bool = True) -> list[dict]:
    """Sync every configured remote source (or just ``only``)."""
    reports = []
    for src in configured_sources():
        if only and (src.get("name") or "") != only:
            continue
        if verbose:
            print(f"→ {src.get('name')} ({src.get('kind')})")
        reports.append(sync_source(src, force=force, verbose=verbose))
    return reports


# ── Statistics ───────────────────────────────────────────────────────────────


def rejection_stats(source: str | None = None, top: int = 20) -> list[dict]:
    """Most-rejected paths first, with reason and how often. Includes flags.

    This is the answer to "make the opt-in visible": it separates *rejected*
    (not indexed) from *flagged* (indexed with a warning), so neither hides
    behind the other.
    """
    conn = _connect()
    try:
        params: list = []
        where = ""
        if source:
            where = "WHERE source = ?"
            params.append(source)
        rows = conn.execute(
            f"""
            SELECT source, path, reason, count, last_at, detail
            FROM source_rejects
            {where}
            ORDER BY count DESC, last_at DESC
            LIMIT ?
            """,
            (*params, top),
        ).fetchall()
        out = []
        for r in rows:
            kind = "flagged" if r["reason"] in FLAG_REASONS else "rejected"
            out.append(
                {
                    "source": r["source"],
                    "path": r["path"],
                    "reason": r["reason"],
                    "kind": kind,
                    "count": int(r["count"]),
                    "last_at": r["last_at"],
                    "detail": r["detail"] or "",
                }
            )
        return out
    finally:
        conn.close()


def format_rejection_stats(rows: list[dict], source: str | None = None) -> str:
    """Human-readable table for the CLI. Always says what the numbers mean."""
    head = f"Rejected / flagged pages{' for ' + source if source else ''}"
    if not rows:
        return (
            f"{head}\n"
            "  none recorded — no page has been skipped in a sync so far\n"
            "  (an empty table is NOT proof of health: it accumulates only from\n"
            "   syncs that actually ran)"
        )
    lines = [head, f"  {'kind':8} {'count':>6}  {'source':14} {'reason':20} path"]
    for r in rows:
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(r["last_at"] or 0))
        lines.append(
            f"  {r['kind']:8} {r['count']:>6}  {r['source'][:14]:14} {r['reason']:20}"
            f" {r['path'][:60]}  (last {when})"
        )
    lines.append("")
    lines.append("  rejected = not indexed  ·  flagged = indexed, served with a warning")
    return "\n".join(lines)


def flagged_counts() -> dict[str, int]:
    """{source: number of flagged pages} for sources with page metadata."""
    try:
        conn = _connect()
    except Exception:
        return {}
    try:
        rows = conn.execute(
            "SELECT source, COUNT(*) AS n FROM page_meta WHERE flagged = 1 GROUP BY source"
        ).fetchall()
        return {r["source"]: int(r["n"]) for r in rows}
    except Exception:
        return {}
    finally:
        conn.close()


def page_provenance(source: str, path: str) -> dict | None:
    """Provenance for one page, or None for locally indexed pages."""
    try:
        conn = _connect()
    except Exception:
        return None
    try:
        row = conn.execute(
            "SELECT fetched_at, flagged, flag_reason FROM page_meta WHERE source = ? AND path = ?",
            (source, path),
        ).fetchone()
        return dict(row) if row else None
    except Exception:
        return None
    finally:
        conn.close()


def remote_sources_with_meta() -> dict[str, float]:
    """{source: last fetched_at} for every remote source in the KB."""
    try:
        conn = _connect()
    except Exception:
        return {}
    try:
        rows = conn.execute(
            "SELECT source, MAX(fetched_at) AS f FROM page_meta GROUP BY source"
        ).fetchall()
        return {r["source"]: (r["f"] or 0.0) for r in rows}
    except Exception:
        return {}
    finally:
        conn.close()
