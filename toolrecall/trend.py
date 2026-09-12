"""Daily hit-rate trend from the access_log — self-serve cache verification.

Answers the user question "how good is toolrecall for MY use case?" with a
time dimension, which lifetime counters (`toolrecall stats`) cannot give:
a 62% day and an 82% day average into an invisible mush, and a never-
cacheable read class silently caps the headline rate (the 2026-09 plateau:
session transcripts at ~28% of reads / 27% hit rate pinned the global rate
at ~71% while the cacheable classes ran at ~80%+).

Design (deliberately stateless, zero-dep):
  - Computes daily aggregates from the ``access_log`` table at query time.
    No rollup tables, no background aggregation, no cron — access_log is
    a rolling ~50k-entry window and that IS the retention policy.
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


def daily_trend(days: int | None = None) -> list[dict]:
    """Per-day file_cache hit/miss stats from access_log, oldest first.

    Args:
        days: Limit to the most recent N days (None = all in window).

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
            WHERE category = 'file_cache'
            GROUP BY d
            ORDER BY d
            """
        ).fetchall()
    finally:
        conn.close()

    out = []
    for d, reads, hits, misses in rows:
        reads, hits, misses = int(reads), int(hits or 0), int(misses or 0)
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
            """
            SELECT path, COUNT(*) AS reads, SUM(hit) AS hits
            FROM access_log
            WHERE category = 'file_cache' AND path IS NOT NULL AND path != ''
            GROUP BY path
            """
        ).fetchall()
    finally:
        conn.close()

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
            "SELECT MIN(cached_at), MAX(cached_at), COUNT(*) FROM access_log"
        ).fetchone()
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
        "oldest": fmt(lo),
        "newest": fmt(hi),
    }


def trend_report(days: int | None = 7, as_json: bool = False) -> str:
    """Human or JSON report combining trend + miss classes + coverage."""
    trend = daily_trend(days)
    classes = miss_classes()
    cov = coverage()
    if as_json:
        return json.dumps({"trend": trend, "miss_classes": classes, "coverage": cov}, indent=2)

    lines = []
    lines.append("  ToolRecall Hit-Rate Trend (daily, from access_log)")
    lines.append("  " + "=" * 54)
    if not trend:
        lines.append("  No access_log data yet — the trend fills in as reads happen.")
        return "\n".join(lines)

    lines.append(
        f"  coverage: {cov['oldest']} → {cov['newest']} "
        f"({cov['window_entries']}/{cov['window_max']} window entries)"
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
    if classes:
        lines.append("  Worst hit-rate classes (min 100 reads in window):")
        lines.append(
            "  path_class                                             reads    share   hit%"
        )
        lines.append("  " + "-" * 72)
        for c in classes:
            lines.append(
                f"  {c['path_class']:<54} {c['reads']:>6}  {c['share_pct']:>5}%  {c['hit_pct']:>5}"
            )
        lines.append("")
        lines.append("  A class with a persistently low hit% and a large share is either")
        lines.append("  rewritten every access (candidate for a [cache].file_ttls = 0 pin")
        lines.append("  so it exits the miss accounting honestly) or genuinely churning.")
    return "\n".join(lines)
