"""Daily hit-rate trend from the access_log — self-serve cache verification.

Answers the user question "how good is toolrecall for MY use case?" with a
time dimension, which lifetime counters (`toolrecall stats`) cannot give:
a 62% day and an 82% day average into an invisible mush, and a never-
cacheable read class silently caps the headline rate (the 2026-09 plateau:
session transcripts at ~28% of reads / 27% hit rate pinned the global rate
at ~71% while the cacheable classes ran at ~80%+).

Design (deliberately stateless, zero-dep):
  - Computes daily aggregates at query time from two plain tables: the live
    ``access_log`` (a rolling ~50k-entry window — that IS the live retention
    policy) and ``access_log_banked``, the compacted per-day/per-path remains
    of windows a ``reset_stats()`` moved aside rather than deleted. No
    background aggregation, no cron, no cache: both are read directly, and
    the infra filter applies to identically-shaped rows from either source, so
    a banked day is comparable to a live one.
  - Reads the DB directly (read-only), never through the daemon: a CLI
    reporting command must not depend on (or perturb) the daemon.
  - Alongside the daily rate, reports the worst miss-heavy path classes —
    the diagnostic that actually found the plateau bug.

Never-cacheable classes: a path class with a persistently low hit rate and
a large read share is either (a) rewritten every access (transcripts,
logs) — candidates for a ``[cache].file_ttls = 0`` pin so they exit the
accounting honestly — or (b) genuinely churning content, in which case the
low rate is the truth about the workload.
"""

import json
import os
import sqlite3

# Rolling window size of access_log (kept trimmed by cache._record).
# Used only for the "coverage" note in output; the query itself is unbounded.
_ACCESS_LOG_WINDOW = 50_000

# Minimum reads for a path class to be called out in the breakdown. Below
# this the class is statistical noise and would only clutter the report.
_MIN_CLASS_READS = 100


def _connect_readonly() -> sqlite3.Connection:
    """Read-only connection to the cache DB (WAL-safe, no lock contention).

    Reads config FRESH per call (load_config(), not the module singleton):
    under pytest, sibling modules imported earlier capture the singleton
    before this test's TOOLRECALL_CACHE_DB env pin lands — the singleton
    would point the trend at the wrong (or production) DB. A fresh load
    costs one small TOML read per CLI invocation and is order-independent.
    """
    from toolrecall.config import load_config
    from toolrecall.storage import active_db_path

    db_path = active_db_path(load_config())
    # Ensure the DB + schema exist before opening read-only: a fresh install
    # (or a first-ever `trend` call) has no cache.db yet, and mode=ro fails
    # on a missing file. Same init the cache layer performs, so the trend
    # command can never be the thing that creates a schema-less DB.
    from toolrecall._db import _init as _db_init
    from toolrecall.cache import SCHEMA  # module-level schema constant

    _db_init(SCHEMA)
    uri = f"file:{db_path}?mode=ro"
    return sqlite3.connect(uri, uri=True, timeout=5)


def _day_expr() -> str:
    return "date(cached_at, 'unixepoch')"


def savings_trend(days: int | None = 30, usage_csv: str | None = None) -> list[dict]:
    """Per-day LLM savings from the proxy usage CSV, oldest first.

    A separate source from ``daily_trend`` on purpose. access_log is a
    rolling 50k-row window that Hermes-style infrastructure polling fills
    in ~2 days (2026-09-22: 47,931 of 50,000 rows were ~/.hermes config,
    cron and plugin manifest reads), so it cannot carry history — and its
    numbers are disk I/O, not tokens withheld from a model. The usage CSV
    is append-only and already holds 50 days of per-request truth.

    Returns [{date, calls, sent, not_sent, dedup, replay_calls,
    replay_tokens, cost_usd}, ...] where:
      sent          prompt tokens the model actually received
      not_sent      dedup + replay_tokens (what ToolRecall withheld)
      replay_tokens prompt tokens of calls answered wholly from cache
      cost_usd      provider-billed dollars (HIT rows excluded: their cost
                    column is the cached body's replayed price)
    """
    import csv as _csv
    from datetime import datetime, timezone

    path = usage_csv or os.path.join(os.path.expanduser("~"), ".toolrecall", "proxy_usage.csv")
    if not os.path.isfile(path):
        return []
    agg: dict[str, dict] = {}
    try:
        with open(path, errors="replace") as f:
            for row in _csv.DictReader(f):
                try:
                    ts = float(row.get("timestamp") or 0)
                except (TypeError, ValueError):
                    continue
                target = (row.get("target_path") or "").strip()
                # Model calls only — /models, /props are metadata traffic and
                # would dilute a savings series with rows that cost nothing.
                if "/chat/completions" not in target and "/messages" not in target:
                    continue
                day = datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")
                slot = agg.setdefault(
                    day,
                    {
                        "date": day,
                        "calls": 0,
                        "sent": 0,
                        "dedup": 0,
                        "replay_calls": 0,
                        "replay_tokens": 0,
                        "cost_usd": 0.0,
                    },
                )
                try:
                    prompt = int(row.get("prompt_tokens") or 0)
                except (TypeError, ValueError):
                    prompt = 0
                slot["calls"] += 1
                if (row.get("cache_status") or "").strip().upper() == "HIT":
                    # Answered from cache: never sent, never charged again.
                    slot["replay_calls"] += 1
                    slot["replay_tokens"] += prompt
                    continue
                slot["sent"] += prompt
                try:
                    slot["dedup"] += int(row.get("dedup_tokens") or 0)
                except (TypeError, ValueError):
                    pass
                try:
                    slot["cost_usd"] += float(row.get("cost_usd") or 0.0)
                except (TypeError, ValueError):
                    pass
    except OSError:
        return []
    out = []
    for day in sorted(agg):
        slot = agg[day]
        slot["not_sent"] = slot["dedup"] + slot["replay_tokens"]
        base = slot["sent"] + slot["not_sent"]
        slot["saved_pct"] = round(100.0 * slot["not_sent"] / base, 1) if base else None
        out.append(slot)
    return out[-days:] if days else out


def _banked_rows(conn: sqlite3.Connection, category: str) -> list[tuple]:
    """(day, path, reads, hits, misses) from ``access_log_banked``, infra-filtered.

    These are the rows a prior ``reset_stats()`` compacted out of the live
    window. Without them the trend claims the window starts at the last reset
    — the reset used to DELETE them, which is what made a reset silently blank
    the report. Returns [] when the table is absent (pre-migration DB) so a
    read-only trend never fails on an old file.
    """
    if not conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='access_log_banked'"
    ).fetchone():
        return []
    return conn.execute(
        f"""
        SELECT day, path, reads, hits, misses
        FROM access_log_banked
        WHERE category = ?
          AND {_infra_filter_sql()}
        """,
        (category,),
    ).fetchall()


def _infra_filter_sql(column: str = "path") -> str:
    """SQL predicate excluding agent-infrastructure reads from a trend.

    Not a judgement about the cache — these reads are cached correctly and
    their I/O saving is real (it stays in cache_stats). They are excluded
    from the *trend* for two measured reasons (2026-09-22):

      1. Volume. Gateway polling of its own config, cron jobs and plugin
         manifests was 47,931 of the 50,000 access_log rows — it evicted
         the user's real history in ~2 days, leaving a 2-day trend.
      2. Meaning. These files never reach a model's context, so their hit
         rate says nothing about the question a trend is asked: is my
         agent's work getting cheaper?

    Kept deliberately narrow: only the agent's own plumbing, matched by
    location, never by file type — a user's YAML under their own project
    is real work and must stay in.
    """
    return (
        f"({column} NOT LIKE '%/.hermes/%' "
        f"AND {column} NOT LIKE '%/plugin.yaml' "
        f"AND {column} NOT LIKE '%/.toolrecall/%')"
    )


def daily_trend(days: int | None = None, category: str = "file_cache") -> list[dict]:
    """Per-day cache hit/miss stats from access_log, oldest first.

    Args:
        days: Limit to the most recent N days (None = all in window).
        category: access_log category to aggregate. Default ``file_cache``;
            ``mcp_cache`` is the other series worth showing (MCP replays write a
            synthetic ``mcp:<server>/<tool>`` path — see cache._record). Kept as
            a parameter rather than a union so the two series are never summed:
            a file read and an MCP replay are different kinds of saving.

    Returns:
        List of dicts: {date, reads, hits, misses, hit_pct}.
    """
    conn = _connect_readonly()
    try:
        rows = conn.execute(
            f"""
            SELECT {_day_expr()} AS d,
                   COUNT(*)                    AS reads,
                   SUM(hit)                    AS hits,
                   SUM(1 - hit)                AS misses
            FROM access_log
            WHERE category = ?
              AND {_infra_filter_sql()}
            GROUP BY d
            ORDER BY d
            """,
            (category,),
        ).fetchall()
        banked = _banked_rows(conn, category)
    finally:
        conn.close()

    # Live and banked rows are disjoint by construction: a reset compacts the
    # window and drops the raw rows in the same transaction, so adding them can
    # never count one read twice.
    agg: dict[str, list[int]] = {}
    for d, reads, hits, misses in rows:
        slot = agg.setdefault(d, [0, 0, 0])
        slot[0] += int(reads)
        slot[1] += int(hits or 0)
        slot[2] += int(misses or 0)
    for d, _path, reads, hits, misses in banked:
        slot = agg.setdefault(d, [0, 0, 0])
        slot[0] += int(reads)
        slot[1] += int(hits or 0)
        slot[2] += int(misses or 0)

    out = []
    for d in sorted(agg):
        reads, hits, misses = agg[d]
        out.append(
            {
                "date": d,
                "reads": reads,
                "hits": hits,
                "misses": misses,
                "hit_pct": round(100.0 * hits / reads, 1) if reads else None,
            }
        )
    return out[-days:] if days else out


def miss_classes(min_reads: int = _MIN_CLASS_READS, top: int = 5) -> list[dict]:
    """Path classes with the worst hit rates in the access_log window.

    A "class" is the second-level directory of the path (e.g.
    ``~/.hermes/webui`` from ``~/.hermes/webui/sessions/x.json``) — coarse
    enough to aggregate, specific enough to act on. Only classes with at
    least ``min_reads`` reads are reported; below that a class is noise.

    Returns:
        List of dicts: {path_class, reads, hits, hit_pct, share_pct},
        worst (lowest hit_pct) first, at most ``top`` entries.
    """
    conn = _connect_readonly()
    try:
        rows = conn.execute(
            f"""
            SELECT path, COUNT(*) AS reads, SUM(hit) AS hits
            FROM access_log
            WHERE category = 'file_cache' AND path IS NOT NULL AND path != ''
              AND {_infra_filter_sql()}
            GROUP BY path
            """
        ).fetchall()
        banked = _banked_rows(conn, "file_cache")
    finally:
        conn.close()

    # Paths from a compacted window are the same kind of evidence as live ones:
    # a reset must not silently shrink the class breakdown either.
    rows = list(rows) + [(p, r, h) for _d, p, r, h, _m in banked if p]

    # Aggregate per class in Python — path normalization is easier to keep
    # honest outside SQL, and per-path counts stay available for future
    # drill-down without a second query.
    # Class key = directory portion of the path, capped at 5 components
    # (e.g. /home/u/.hermes/webui from /home/u/.hermes/webui/sessions/x.json,
    # /tmp from /tmp/tiny0.txt). Keying on the file path itself would make
    # every distinct file its own class of 1 read — pure noise.
    classes: dict[str, list[int]] = {}
    for path, reads, hits in rows:
        dir_parts = os.path.dirname(os.path.normpath(path)).split(os.sep)
        cls = os.sep.join(dir_parts[:5])
        entry = classes.setdefault(cls, [0, 0])
        entry[0] += int(reads)
        entry[1] += int(hits or 0)

    total_reads = sum(v[0] for v in classes.values()) or 1
    out = []
    for cls, (reads, hits) in classes.items():
        if reads < min_reads:
            continue
        out.append(
            {
                "path_class": cls,
                "reads": reads,
                "hits": hits,
                "hit_pct": round(100.0 * hits / reads, 1) if reads else None,
                "share_pct": round(100.0 * reads / total_reads, 1),
            }
        )
    out.sort(key=lambda e: (e["hit_pct"] if e["hit_pct"] is not None else 999,))
    return out[:top]


def coverage() -> dict:
    """What time range the trend currently covers (window honesty)."""
    conn = _connect_readonly()
    try:
        row = conn.execute(
            f"SELECT MIN(cached_at), MAX(cached_at), COUNT(*) FROM access_log "
            f"WHERE {_infra_filter_sql()}"
        ).fetchone()
        total = conn.execute("SELECT COUNT(*) FROM access_log").fetchone()[0]
        has_bank = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='access_log_banked'"
        ).fetchone()
        banked = (
            conn.execute(
                "SELECT COUNT(DISTINCT day), MIN(day), MAX(day), SUM(reads),"
                " SUM(hits) FROM access_log_banked"
            ).fetchone()
            if has_bank
            else (0, None, None, 0, 0)
        )
    finally:
        conn.close()
    from datetime import datetime, timezone

    lo, hi, n = row
    fmt = lambda ts: (  # noqa: E731
        datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M") if ts else "-"
    )
    return {
        "window_entries": int(n or 0),
        "window_max": _ACCESS_LOG_WINDOW,
        # How much of the rolling window is agent plumbing rather than work.
        # High values explain a short trend: infra polling evicts history.
        "infra_entries": max(0, int(total or 0) - int(n or 0)),
        "oldest": fmt(lo),
        "newest": fmt(hi),
        # Days a reset moved out of the live window. Reported separately, never
        # folded into the window counts: "how much of the rolling window is
        # left" and "how far back does the history reach" are different facts.
        "banked_days": int(banked[0] or 0),
        "banked_oldest": banked[1],
        "banked_newest": banked[2],
        "banked_reads": int(banked[3] or 0),
        "banked_hits": int(banked[4] or 0),
    }


def trend_report(days: int | None = 7, as_json: bool = False) -> str:
    """Human or JSON report combining trend + miss classes + coverage."""
    trend = daily_trend(days)
    classes = miss_classes()
    cov = coverage()
    savings = savings_trend(days if days else 30)
    if as_json:
        return json.dumps(
            {
                "savings_trend": savings,
                "trend": trend,
                "miss_classes": classes,
                "coverage": cov,
            },
            indent=2,
        )

    lines = []
    # ── LLM savings (usage CSV: append-only, carries real history) ──
    lines.append("  Tokens NOT sent to the model (daily, from the usage log)")
    lines.append("  " + "=" * 54)
    if savings:
        lines.append("  date          calls          sent      not sent   saved     cost")
        lines.append("  " + "-" * 66)
        for s in savings:
            pct = f"{s['saved_pct']:.1f}%" if s["saved_pct"] is not None else "    -"
            lines.append(
                f"  {s['date']}  {s['calls']:>7,}  {s['sent']:>12,}  {s['not_sent']:>12,}"
                f"  {pct:>6}  ${s['cost_usd']:>7.2f}"
            )
        _ns = sum(s["not_sent"] for s in savings)
        _rc = sum(s["replay_calls"] for s in savings)
        lines.append("")
        lines.append(
            f"  {_ns:,} tok withheld over {len(savings)} days"
            f" — {_rc:,} calls answered from cache, the rest repeated text removed."
        )
    else:
        lines.append("  No model calls logged yet — this fills in as your agent works.")
    lines.append("")

    # ── Disk I/O hit rate (access_log: rolling window, short history) ──
    lines.append("  Disk reads served from cache (I/O, not tokens)")
    lines.append("  " + "=" * 54)
    if not trend:
        lines.append("  No access_log data yet — the trend fills in as reads happen.")
        return "\n".join(lines)

    lines.append(
        f"  coverage: {cov['oldest']} → {cov['newest']} "
        f"({cov['window_entries']}/{cov['window_max']} window entries)"
    )
    if cov.get("infra_entries"):
        lines.append(
            f"  ({cov['infra_entries']:,} agent-infrastructure reads excluded —"
            f" config/cron/plugin polling, never sent to a model)"
        )
    if cov.get("banked_days"):
        lines.append(
            f"  (+ {cov['banked_days']} day(s) before this window kept as banked totals:"
            f" {cov['banked_reads']:,} reads, {cov['banked_hits']:,} hits,"
            f" oldest {cov['banked_oldest']})"
        )
    lines.append("")
    lines.append("  date        reads   hits  misses  hit%")
    lines.append("  " + "-" * 42)
    for t in trend:
        lines.append(
            f"  {t['date']}  {t['reads']:>6}  {t['hits']:>6}  {t['misses']:>6}  {t['hit_pct']:>5}"
            if t["hit_pct"] is not None
            else f"  {t['date']}  {t['reads']:>6}  {t['hits']:>6}  {t['misses']:>6}    -"
        )
    lines.append("")
    lines.append("  Worst hit-rate classes (min 100 reads in window):")
    lines.append("  path_class                                             reads    share   hit%")
    lines.append("  " + "-" * 72)
    for c in classes:
        # path_class can exceed the 54-char column (deep paths) — f-string
        # width is a MINIMUM, so an over-long class pushes the numeric
        # columns right and mangles the table (no truncation happened).
        cls = c["path_class"]
        if len(cls) > 54:
            cls = "…" + cls[-53:]  # keep the tail: the distinctive part
        lines.append(f"  {cls:<54} {c['reads']:>6}  {c['share_pct']:>5}%  {c['hit_pct']:>5}")
    if classes:
        lines.append("")
        lines.append("  A class with a persistently low hit% and a large share is either")
        lines.append("  rewritten every access (candidate for a [cache].file_ttls = 0 pin")
        lines.append("  so it exits the miss accounting honestly) or genuinely churning.")
    return "\n".join(lines)
