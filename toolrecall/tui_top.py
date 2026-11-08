"""toolrecall top — zero-dependency interactive TUI dashboard (stdlib curses).

Views:
  1  Cache stats — per-cache hits/misses/hit-rate/token accounting
  2  Access log — scrollable recent request log (HIT/MISS colored)
  3  Context tracker — dirty/clean/stale files; Enter refreshes a stale file
  4  Help / keybindings

Data sources: daemon UDS when reachable ("cache_status", "context_*",
"cache_refresh_file"); read-only SQLite fallback for stats/log when the
daemon is down. Actions (refresh file, set checkpoint) require the daemon.

Read-only by design: no invalidate-all/reset-keys exposed.

Usage:
    toolrecall top [--interval SECONDS] [--db PATH] [--once]
Non-tty runs print a plain-text snapshot (stats + context summary).
"""

from __future__ import annotations

import argparse
import curses
import logging
import os
import re
import sqlite3
import sys
import time
from typing import Any

try:
    from .transport import TransportClient, DEFAULT_PATH
    from .config import load_config
except ImportError:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from toolrecall.transport import TransportClient, DEFAULT_PATH  # type: ignore
    from toolrecall.config import load_config  # type: ignore

CATEGORIES = [
    "file_cache",
    "terminal_cache",
    "mcp_cache",
    "api_cache",
    "skill_cache",
    "write_cache",
    "script_cache",
    "code_cache",
    "browser_cache",
]

HELP_TEXT = [
    "toolrecall top — interactive dashboard (read-only)",
    "",
    "1/2/3/4   switch view: saved (money+measured) / access log / trend / stats",
    "tab       cycle views",
    "up/down   scroll (log, context views); move selection",
    "enter     context view: refresh the selected stale file on disk",
    "c         context view: set a new checkpoint (context tracker)",
    "r         force refresh now",
    "q / esc   quit",
    "",
    "METRIC TIERS (don't conflate):",
    "  IO      — hits/rate, tokens served cache vs disk: latency only,",
    "            NOT money. Content reaches the LLM either way.",
    "  CONTEXT — measured not-sent: wire-verified tokens that never",
    "            reached the LLM — stubbed re-reads + within-request",
    "            dedup (disjoint sets; the saved view sums both).",
    "            Estimates: `toolrecall stats`.",
    "  MONEY   — billed input/output tokens: only from provider usage",
    "            via the API proxy. No proxy traffic = no data.",
    "",
    "Data: daemon UDS when up, read-only SQLite fallback when down.",
    "Actions need the daemon; they are safe (no cache-wide invalidation).",
]


def human_tokens(n: int) -> str:
    n = int(n)
    if abs(n) >= 1_000_000_000:
        return f"{n / 1_000_000_000:.1f}B"
    if abs(n) >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if abs(n) >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def age_str(ts: float) -> str:
    if not ts:
        return "-"
    d = max(0, int(time.time() - ts))
    if d < 60:
        return f"{d}s"
    if d < 3600:
        return f"{d // 60}m"
    if d < 86400:
        return f"{d // 3600}h"
    return f"{d // 86400}d"


def resolve_db(explicit: str | None) -> str:
    if explicit:
        return os.path.expanduser(explicit)
    env = os.environ.get("TOOLRECALL_CACHE_DB")
    if env:
        return env
    try:
        return load_config().cache_db
    except Exception:  # noqa: BLE001 — config load must never break the dashboard
        return os.path.join(os.path.expanduser("~"), ".toolrecall", "cache.db")


def _daemon_cmd(payload: dict, timeout: float = 3.0) -> dict | None:
    """Send one command to the daemon. None on any failure."""
    try:
        tc = TransportClient(DEFAULT_PATH)
        resp = tc.send(payload, timeout=timeout)
    except Exception:  # noqa: BLE001 — any transport failure = fallback mode
        return None
    if isinstance(resp, dict) and "error" in resp:
        return {"_error": resp["error"]}
    return resp if isinstance(resp, dict) else None


# ─── Data gathering ──────────────────────────────────────────


def stats_via_daemon() -> dict | None:
    """Live stats over UDS. Returns dict with _daemon=True, or None."""
    resp = _daemon_cmd({"cmd": "cache_status"})
    if resp is None or "_error" in resp:
        return None
    result = resp.get("result")
    if isinstance(result, dict):
        out = dict(result)
        out["_daemon"] = True
        return out
    return None


def _open_ro(db: str):
    """Open a cache.db read-only, with a copy-snapshot fallback.

    Plain ``mode=ro`` fails on a WAL database when the -shm index is
    missing/uncreatable (e.g. directory not writable, shm deleted by a
    cleaner while no writer exists to rebuild it) -> sqlite3.Error
    "unable to open database file". Fallback: copy db + -wal into a
    writable temp dir and read there — recovers committed-but-
    uncheckpointed WAL frames without touching the original.

    Returns a connection, or None if both paths fail.
    """
    try:
        con = sqlite3.connect("file:" + db + "?mode=ro", uri=True)
        # connect() is LAZY and even 'SELECT 1' is a constant — probe with a
        # statement that forces a real page read (sqlite_master scan), which
        # is where the ro/WAL -shm failure actually surfaces.
        con.execute("SELECT count(*) FROM sqlite_master").fetchone()
        return con
    except sqlite3.Error:
        try:
            con.close()
        except Exception as _close_err:  # noqa: BLE001 — close-failure carries no signal
            logging.getLogger("toolrecall.tui").debug("snapshot con.close failed: %s", _close_err)
    import shutil
    import tempfile

    try:
        snapdir = tempfile.mkdtemp(prefix="tr_top_read_")
        snap = os.path.join(snapdir, "cache.db")
        shutil.copy(db, snap)
        for suffix in ("-wal",):
            sidecar = db + suffix
            if os.path.exists(sidecar):
                try:
                    shutil.copy(sidecar, snap + suffix)
                except OSError:
                    pass
        con = sqlite3.connect("file:" + snap + "?mode=ro", uri=True)
        try:
            con.execute("SELECT 1").fetchone()
        except sqlite3.Error:
            return None
        try:
            con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.Error:
            pass
        return con
    except (OSError, sqlite3.Error):
        return None


def stats_via_sqlite(db: str) -> dict | None:
    """Read-only SQLite fallback. Returns dict with _daemon=False, or None."""
    stats: dict[str, Any] = {}
    con = _open_ro(db)
    if con is None:
        return None
    try:
        rows = con.execute(
            "SELECT category, hits, misses, tokens_saved,"
            " context_tokens_saved, updated_at FROM cache_stats"
        ).fetchall()
        for cat, hits, misses, t_saved, ctx_saved, upd in rows:
            total = hits + misses
            hr = (str(round(hits / total * 100)) + "%") if total else "-"
            stats[cat] = {
                "hits": hits,
                "misses": misses,
                "tokens_served_from_cache": t_saved,
                "tokens_not_sent_to_llm": ctx_saved,
                "hit_rate": hr,
                "updated_at": upd,
            }
        out = dict(stats)
        out["_daemon"] = False
        return out
    except sqlite3.Error:
        return None
    finally:
        con.close()


def access_rows(db: str, limit: int = 200) -> list:
    """Recent access_log rows, newest last. [] if unavailable."""
    con = _open_ro(db)
    if con is None:
        return []
    try:
        rows = con.execute(
            "SELECT category, COALESCE(path, ''), hit, tokens, cached_at"
            " FROM access_log ORDER BY id DESC LIMIT " + str(int(limit))
        ).fetchall()
        rows.reverse()
        return rows
    except sqlite3.Error:
        return []
    finally:
        con.close()


def money_today(usage_csv: str | None = None) -> dict | None:
    """Billed-truth aggregation from the proxy usage CSV (today, UTC).

    Returns None when the CSV is absent or has no rows today — the honest
    'no proxy traffic' state. Fields:
      requests, prompt, completion, replays (cache_status=HIT),
      median (prompt-token median — the re-sent-history diagnostic),
      dedup_tokens (wire-verified tokens removed from requests pre-billing
      — the per-request Y in "sent X, not sent Y").
    """
    path = usage_csv or os.path.join(os.path.expanduser("~"), ".toolrecall", "proxy_usage.csv")
    if not os.path.isfile(path):
        return None
    import csv as _csv
    from datetime import datetime, timezone

    requests = prompt = completion = replays = 0
    sizes = []
    dedup_tokens = 0
    cost = 0.0
    cost_rows = 0
    day_start = (
        datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    )
    try:
        with open(path, errors="replace") as f:
            for row in _csv.DictReader(f):
                try:
                    ts = float(row.get("timestamp") or 0)
                    p = int(row.get("prompt_tokens") or 0)
                except ValueError:
                    continue
                if ts < day_start:
                    continue
                requests += 1
                prompt += p
                completion += int(row.get("completion_tokens") or 0)
                if (row.get("cache_status") or "").strip().upper() == "HIT":
                    replays += 1
                if p > 0:
                    sizes.append(p)
                try:
                    dedup_tokens += int(row.get("dedup_tokens") or 0)
                except (TypeError, ValueError):
                    pass
                try:
                    c = float(row.get("cost_usd") or 0.0)
                except (TypeError, ValueError):
                    c = 0.0
                else:
                    cost += c
                if c > 0:
                    cost_rows += 1
    except OSError:
        return None
    if not requests:
        return None
    sizes.sort()
    median = sizes[len(sizes) // 2] if sizes else 0
    return {
        "requests": requests,
        "prompt": prompt,
        "completion": completion,
        "replays": replays,
        "median": median,
        "dedup_tokens": dedup_tokens,
        # Provider-reported dollars summed over rows that carried a cost.
        # cost_rows == 0 means the column held no provider cost at all
        # (pre-v2 rows, estimate fallbacks) — report None, not $0.00.
        "cost_usd": cost if cost_rows else None,
    }


def stubbed_today(db: str) -> tuple[int, int]:
    """Today-scoped wire-verified stub tokens from access_log (+ all-days).

    The bridge logs every dedup-stub event to access_log under the
    ``mcp_dedup`` category (daemon cmd ``dedup_tokens``). That per-day
    attribution is what the cumulative ``cache_stats.tokens_not_sent_measured``
    counter lacks — the saved view used to print that cumulative number
    under a "today" header (12.6M next to a 15.9M billed-today; most of it
    earned on earlier days). Sums the log instead:

    Returns (today_tokens, window_tokens); day boundary = UTC midnight,
    matching money_today(). (0, 0) when the db/log is unavailable — callers
    treat that as "no stub data", never as zero savings.
    """
    con = _open_ro(db)
    if con is None:
        return 0, 0
    try:
        rows = con.execute(
            "SELECT cached_at, tokens FROM access_log WHERE category = 'mcp_dedup'"
        ).fetchall()
    except sqlite3.Error:
        return 0, 0
    finally:
        con.close()
    from datetime import datetime, timezone

    day_start = (
        datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    )
    today = 0
    window = 0
    for ts, tokens in rows:
        try:
            tokens = int(tokens or 0)
        except (TypeError, ValueError):
            continue
        window += tokens
        if (ts or 0) >= day_start:
            today += tokens
    return today, window


def blocked_reads_today(log_path: str | None = None) -> list[dict]:
    """Blocked reads today (sensitive-file / policy-denied), from the
    daemon log. Bounded tail (8k lines), today only, deduped per path
    with count + current size (content never reached the LLM — the
    size shown is what the security gate kept out).
    """
    path = log_path or os.path.join(
        os.path.expanduser("~"), ".toolrecall", "logs", "toolrecall.log"
    )
    if not os.path.isfile(path):
        return []
    day_start = time.mktime(time.strptime(time.strftime("%Y-%m-%d"), "%Y-%m-%d"))
    blocked: dict[str, dict] = {}
    try:
        with open(path, errors="replace") as f:
            tail = f.readlines()[-8000:]
    except OSError:
        return []
    rx = re.compile(
        r"^(\S+ \S+) \w+ toolrecall\.\w+ "
        r"(?:Blocked read of sensitive file: (.+?) \(matched"
        r"|Blocked path not in allowed_paths[^:]*: )(.+)$"
    )
    for line in tail:
        if "Blocked" not in line:
            continue
        m = rx.match(line)
        if not m:
            continue
        path = (m.group(2) or m.group(3) or "").strip()
        if not path:
            continue
        try:
            ts = time.mktime(time.strptime(m.group(1), "%Y-%m-%d %H:%M:%S"))
        except ValueError:
            continue
        if ts < day_start:
            continue
        entry = blocked.setdefault(path, {"path": path, "count": 0, "last": 0.0})
        entry["count"] += 1
        entry["last"] = max(entry["last"], ts)
    out = sorted(blocked.values(), key=lambda e: -e["count"])
    for e in out:
        try:
            e["size_str"] = human_tokens(os.path.getsize(e["path"]))
        except OSError:
            e["size_str"] = "?"  # deleted or permission-denied — still blocked
    return out


def context_via_daemon() -> dict | None:
    """Context tracker snapshot over UDS: stats + stale. None if daemon down.

    Response-shape note: context handlers return the tracker dict at TOP
    level (transport only adds ``v: 1``), unlike cache_status which nests
    under ``result``. Accept both.
    """
    stats = _daemon_cmd({"cmd": "context_get_stats"})
    stale = _daemon_cmd({"cmd": "context_get_stale"})
    if stats is None or "_error" in stats:
        return None
    out = dict(stats.get("result") or {k: v for k, v in stats.items() if k != "v"})
    if stale and "_error" not in stale:
        s = stale.get("result") or {k: v for k, v in stale.items() if k != "v"}
        out["stale"] = s.get("stale", [])
        out["est_reclaimable_tokens"] = s.get("est_reclaimable_tokens", 0)
    else:
        out["stale"] = []
        out["est_reclaimable_tokens"] = 0
    return out


def health_header() -> dict:
    """Cheap health subset: pid alive, socket exists."""
    info = {"pid_alive": False, "socket": False, "pid": ""}
    pid_file = os.path.join(os.path.expanduser("~"), ".toolrecall", "daemon.pid")
    try:
        with open(pid_file) as f:
            pid = f.read().strip()
        info["pid"] = pid
        if pid.isdigit():
            info["pid_alive"] = os.path.exists("/proc/" + pid)
    except FileNotFoundError:
        pass  # no pid file = daemon not running — the normal down case
    except OSError:
        info["pid_alive"] = False
    # os.path.exists() never raises — no handler needed for the socket probe.
    info["socket"] = os.path.exists(DEFAULT_PATH)
    return info


def snapshot_factory(db: str) -> dict:
    """One stats snapshot: daemon stats w/ sqlite fallback."""
    stats = stats_via_daemon()
    if stats is None:
        stats = stats_via_sqlite(db)
    return {"stats": stats, "db": db, "db_path": db}


# ─── Actions (all safe; require daemon) ──────────────────────


def action_refresh_file(path: str) -> tuple[bool, str]:
    """Invalidate + re-read one file via the daemon (safe, ungated)."""
    resp = _daemon_cmd({"cmd": "cache_refresh_file", "path": path}, timeout=10.0)
    if resp is None:
        return False, "daemon unreachable — cannot refresh"
    if "_error" in resp:
        return False, "refresh failed: " + str(resp["_error"])
    if isinstance(resp.get("result"), dict) and resp["result"].get("error"):
        return False, "refresh failed: " + str(resp["result"]["error"])
    return True, "refreshed " + path


def action_set_checkpoint() -> tuple[bool, str]:
    """Mark a context-tracker checkpoint (safe)."""
    resp = _daemon_cmd({"cmd": "context_set_checkpoint"})
    if resp is None:
        return False, "daemon unreachable — cannot set checkpoint"
    if "_error" in resp:
        return False, "checkpoint failed: " + str(resp["_error"])
    return True, "checkpoint set"


# ─── Plain rendering (non-tty) ───────────────────────────────


def render_plain(snapshot: dict) -> str:
    """Plain-text snapshot (non-tty / agentic mode, --once)."""
    lines = []
    st = snapshot.get("stats") or {}
    daemon = st.get("_daemon", False)
    lines.append("toolrecall top  " + time.strftime("%Y-%m-%d %H:%M:%S"))
    lines.append(
        "daemon: %s  db: %s" % ("live" if daemon else "down (sqlite fallback)", snapshot["db"])
    )
    lines.append("")
    lines.append(
        "%-14s %8s %8s %7s %12s %10s"
        % ("cache [IO]", "hits", "misses", "rate", "IO-offload", "last-act")
    )
    for cat in CATEGORIES:
        c = st.get(cat)
        if not c:
            continue
        lines.append(
            "%-14s %8s %8s %7s %12s %10s"
            % (
                cat,
                f"{c['hits']:,}",
                f"{c['misses']:,}",
                c["hit_rate"],
                human_tokens(c["tokens_served_from_cache"]),
                age_str(c.get("updated_at", 0)),
            )
        )
    # End-user ordering (2026-09-16): money truth + measured not-sent FIRST,
    # IO table last (tech detail). Same contract as the curses view order.
    money = money_today()
    stubs_today, stubs_window = stubbed_today(snapshot.get("db_path") or snapshot.get("db", ""))
    if money:
        lines.append("")
        # ── THE headline: sent X, not sent Y (end-user order, 2026-09-16) ──
        # 2026-09-18: Y is the UMBRELLA — stubbed re-reads + within-request
        # dedup, both wire-verified, disjoint sets (stubbed content never
        # reaches the proxy, so the proxy can only dedup what was sent).
        # Percentage base = pre-removal prompt total (billed + removed) —
        # the provider never saw the removed tokens, so "of billed" was
        # the wrong denominator.
        _dy = money.get("dedup_tokens", 0)
        _notsent = _dy + stubs_today
        if _notsent:
            _base = money["prompt"] + _notsent
            _pct = (100.0 * _notsent / _base) if _base else 0.0
            lines.append(
                "sent to LLM today: %s tok  (%d requests)"
                % (human_tokens(money["prompt"]), money["requests"])
            )
            lines.append(
                "not sent (measured): %s tok  —  %.1f%% of would-have-been-billed"
                % (human_tokens(_notsent), _pct)
            )
            lines.append(
                "  · stubbed re-reads: %s tok  (unchanged file re-read → one-line stub)"
                % human_tokens(stubs_today)
            )
            lines.append(
                "  · deduped blocks:   %s tok  (identical blocks within a request)"
                % human_tokens(_dy)
            )
        else:
            lines.append(
                "sent to LLM today: %s tok  (%d requests)  —  no stubbed re-reads,"
                " no duplicate blocks today" % (human_tokens(money["prompt"]), money["requests"])
            )
        lines.append(
            "money [billed via proxy, today]: %s prompt / %s completion tok  replays %d"
            % (
                human_tokens(money["prompt"]),
                human_tokens(money["completion"]),
                money["replays"],
            )
        )
        if money.get("cost_usd") is not None:
            lines.append("  billed cost today: $%.4f (provider-reported)" % money["cost_usd"])
        if money["median"] and money["median"] > 100_000:
            lines.append(
                "  median request %s tok — cost dominated by re-sent history"
                % human_tokens(money["median"])
            )
    elif not daemon:
        lines.append("")
        lines.append("money [billed]: no proxy traffic captured today")
    if stubs_window:
        lines.append("")
        # Today/cumulative split — the old line printed the cumulative
        # counter under a "today" header (scope mismatch the 2026-09-18
        # umbrella change fixes). "logged days" because access_log is a
        # rolling window, not since-forever history.
        lines.append(
            "  stubbed re-reads, logged days: %s  (today: %s — dedup stubs, wire-verified)"
            % (human_tokens(stubs_window), human_tokens(stubs_today))
        )
    ctx = context_via_daemon()
    if ctx:
        lines.append("")
        for e in ctx.get("stale", [])[:10]:
            lines.append("  stale: %s (~%s tok)" % (e["path"], human_tokens(e["est_tokens"])))
    log = snapshot.get("log") or []
    if log:
        lines.append("")
        lines.append("recent access log:")
        for cat, path, hit, tokens, ts in log[-10:]:
            lines.append(
                "  %s  %-13s %-4s %5s  %s"
                % (
                    age_str(ts),
                    cat,
                    "HIT" if hit else "MISS",
                    human_tokens(tokens) if tokens else "-",
                    (path or "")[-60:],
                )
            )
    return "\n".join(lines)


# ─── Curses UI ───────────────────────────────────────────────

VIEW_NAMES = ["context", "log", "trend", "stats", "help"]


def run_curses(scr, snapshot_fn, log_fn, interval: float) -> int:
    """Interactive loop. Returns exit code."""
    curses.curs_set(0)
    scr.nodelay(True)
    try:
        curses.start_color()
        curses.use_default_colors()
        curses.init_pair(1, curses.COLOR_GREEN, -1)
        curses.init_pair(2, curses.COLOR_RED, -1)
        curses.init_pair(3, curses.COLOR_YELLOW, -1)
        curses.init_pair(4, curses.COLOR_CYAN, -1)
    except curses.error:
        pass
    A_HIT = curses.color_pair(1)
    A_MISS = curses.color_pair(2)
    A_DIM = curses.color_pair(3)
    A_SEL = curses.color_pair(4) | curses.A_REVERSE
    A_BOLD = curses.A_BOLD

    state: dict[str, Any] = {
        "view": "context",
        "sel": 0,
        "scroll": 0,
        "snap": None,
        "log": [],
        "ctx": None,
        "stats": None,
        "trend": None,
        "stubs_today": 0,
        "stubs_window": 0,
        "toast": "",
        "toast_ts": 0.0,
        "last": 0.0,
    }

    def toast(msg: str) -> None:
        state["toast"] = msg
        state["toast_ts"] = time.time()

    def refresh_data(force: bool = False) -> None:
        now = time.time()
        if not force and now - state["last"] < interval:
            return
        state["last"] = now
        state["snap"] = snapshot_fn()
        state["log"] = log_fn()
        state["ctx"] = context_via_daemon()
        state["stats"] = state["snap"].get("stats") if isinstance(state["snap"], dict) else None
        # billed-truth aggregation (usage CSV) — cheap file read, once/interval
        try:
            state["money"] = money_today()
        except Exception:  # noqa: BLE001 — money block must never break the TUI
            state["money"] = None
        # day-scoped stub tokens (access_log) — same cadence, never breaks the TUI
        try:
            state["stubs_today"], state["stubs_window"] = stubbed_today(
                (state["snap"] or {}).get("db_path", "")
            )
        except Exception:  # noqa: BLE001 — stub block must never break the TUI
            state["stubs_today"] = state["stubs_window"] = 0
        # trend reads the db directly (like log_fn); cheap + daemon-independent
        try:
            from toolrecall.trend import coverage, daily_trend, miss_classes

            state["trend"] = {
                "days": daily_trend(7),
                "classes": miss_classes(min_reads=10),
                "coverage": coverage(),
            }
        except Exception:  # noqa: BLE001 — trend must never break the dashboard
            state["trend"] = None

    def put(y: int, s: str, attr=0) -> int:
        max_y, max_x = scr.getmaxyx()
        if y < 0 or y >= max_y:
            return y
        try:
            scr.addnstr(y, 0, s[: max_x - 1], max_x - 1, attr)
        except curses.error:
            pass
        return y + 1

    def draw() -> None:
        scr.erase()
        max_y, max_x = scr.getmaxyx()
        view = state["view"]
        snap = state["snap"] or {}
        st = snap.get("stats") or {}
        daemon = st.get("_daemon", False)

        # header: status line, then key hints on their own line (visibility)
        y = put(
            0,
            " toolrecall top  [%s]  daemon: %s  pid %s%s  %ds"
            % (
                view,
                "live" if daemon else "down(sqlite)",
                (state["snap"] and snap and health_header().get("pid")) or "-",
                "" if health_header().get("pid_alive") else " (dead)",
                interval,
            ),
            A_BOLD,
        )
        y = put(y, " 1 saved  2 log  3 trend  4 stats  5 help  r refresh  q quit", A_DIM)
        y = put(y, "", 0)  # gap between chrome and view body

        # wrong-user warning: own db missing but another user's exists
        if view in ("stats", "trend") and not daemon and not any(c in st for c in CATEGORIES):
            db_path = snap.get("db", "?")
            other_db: str | None = None
            for cand in os.listdir("/home") if os.path.isdir("/home") else []:
                if cand == os.environ.get("USER", ""):
                    continue
                other_db = "/home/%s/.toolrecall/cache.db" % cand
                if os.path.exists(other_db):
                    y = put(
                        y,
                        "  ⚠ no cache data for user '%s' — but %s exists (another user's daemon state)"
                        % (os.environ.get("USER", "?"), other_db),
                        A_MISS,
                    )
                    y = put(
                        y,
                        "  daemon state is per-user: run as that user, e.g."
                        " sudo -u %s -i toolrecall top" % cand,
                        A_DIM,
                    )
                    y = put(y, "", 0)
                    break

        if view == "help":
            for line in HELP_TEXT:
                y = put(y, " " + line)
        elif view == "context":
            # ── VIEW 1: SAVED (end-user) — money truth + wire-verified ──
            ctx = state["ctx"]
            y = put(y, " saved today (end-user view — billed truth + measured):", A_BOLD)
            money = state.get("money")
            if money:
                y = put(y + 1, "", 0)  # section gap
                # ── THE headline: sent X, not sent Y (Robin's ask) ──
                # 2026-09-18: Y = umbrella — stubbed re-reads + within-request
                # dedup, both wire-verified and disjoint (stubbed content never
                # reaches the proxy). Pct base = pre-removal prompt total;
                # rationale in render_plain().
                _dy = money.get("dedup_tokens", 0)
                _st = state.get("stubs_today", 0) or 0
                _notsent = _dy + _st
                y = put(
                    y,
                    "  sent to LLM today:      %s tok  (%d requests)"
                    % (human_tokens(money["prompt"]), money["requests"]),
                    A_BOLD,
                )
                if _notsent:
                    _base = money["prompt"] + _notsent
                    _pct = (100.0 * _notsent / _base) if _base else 0.0
                    y = put(
                        y,
                        "  not sent (measured):    %s tok  —  %.1f%% of would-have-been-billed"
                        % (human_tokens(_notsent), _pct),
                        A_HIT,
                    )
                    y = put(
                        y,
                        "  · stubbed re-reads:     %s tok  (unchanged file re-read → one-line stub)"
                        % human_tokens(_st),
                    )
                    y = put(
                        y,
                        "  · deduped blocks:       %s tok  (identical blocks within a request)"
                        % human_tokens(_dy),
                    )
                else:
                    y = put(
                        y,
                        "  not sent (measured):    0 tok  (no stubbed re-reads,"
                        " no duplicate blocks today)",
                        A_DIM,
                    )
                y = put(y, "", 0)
                y = put(
                    y,
                    "  billed via proxy today: %s prompt / %s completion tok"
                    % (human_tokens(money["prompt"]), human_tokens(money["completion"])),
                )
                y = put(y, "  replays (identical prompt served from cache): %d" % money["replays"])
                if money.get("cost_usd") is not None:
                    y = put(
                        y,
                        "  billed cost today: $%.4f (provider-reported, rows carrying cost)"
                        % money["cost_usd"],
                        A_BOLD,
                    )
                if money["median"] and money["median"] > 100_000:
                    y = put(
                        y,
                        "  median request %s tok — cost dominated by re-sent history"
                        % human_tokens(money["median"]),
                        A_MISS,
                    )
            else:
                y = put(y + 1, "", 0)  # section gap
                y = put(
                    y,
                    "  no billed-token data today (LLM traffic does not route"
                    " through the proxy — attach: toolrecall proxy attach)",
                    A_DIM,
                )
            # Cumulative stub ledger, explicitly scoped (2026-09-18): the
            # cache_stats counter is all-days; today's share lives in the
            # umbrella headline above. Printing the cumulative number under
            # a "today" header was the scope mismatch this replaces.
            y = put(y + 1, "", 0)  # section gap
            if state.get("stubs_window"):
                y = put(
                    y,
                    "  stubbed re-reads, logged days: %s  (today: %s — dedup stubs,"
                    " wire-verified)"
                    % (
                        human_tokens(state["stubs_window"]),
                        human_tokens(state["stubs_today"]),
                    ),
                )
            else:
                y = put(
                    y,
                    "  stubbed re-reads: none yet (grows when an unchanged file re-read is stubbed)",
                    A_DIM,
                )
            # Blocked reads: security-gate refusals — also wire-verified
            # never-sent (content never reached the LLM).
            blocked = blocked_reads_today()
            if blocked:
                n_blocked = sum(b["count"] for b in blocked)
                y = put(y + 1, "", 0)  # section gap
                y = put(
                    y,
                    "  blocked reads (never sent to LLM): %d / %d paths"
                    % (n_blocked, len(blocked)),
                    A_BOLD,
                )
                for b in blocked[:4]:
                    y = put(
                        y,
                        "    %4dx  %8s  %s"
                        % (b["count"], b["size_str"], b["path"][: max(24, max_x - 40)]),
                        A_DIM,
                    )
            # IO hint for the curious — one line, no table (view 4 has it)
            _io_saved = 0
            for _cat in ("file_cache", "skill_cache", "terminal_cache"):
                c = (state.get("stats") or {}).get(_cat)
                if isinstance(c, dict):
                    _io_saved += c.get("tokens_served_from_cache", 0)
            if _io_saved:
                y = put(y + 1, "", 0)  # section gap
                y = put(
                    y,
                    "  disk reads avoided (IO bonus, not money): %s" % human_tokens(_io_saved),
                    A_DIM,
                )
        elif view == "stats":
            # ── VIEW 4: per-cache IO stats (tech view) ──
            narrow = max_x < 88
            if narrow:
                # phone/narrow SSH: drop the last-act column (low value)
                y = put(
                    y,
                    " %-14s %8s %8s %6s %10s"
                    % ("cache [IO]", "hits", "miss", "rate", "IO-offload"),
                    A_BOLD,
                )
            else:
                y = put(
                    y,
                    " %-14s %9s %9s %7s %12s %10s"
                    % ("cache [IO]", "hits", "misses", "rate", "IO-offload", "last-act"),
                    A_BOLD,
                )
            if not any(cat in st for cat in CATEGORIES):
                db_path = snap.get("db", "?")
                # multi-user scan: any OTHER user's ~/.toolrecall/cache.db
                other_db = None
                home_root = "/home"
                if os.path.isdir(home_root):
                    for cand in sorted(os.listdir(home_root)):
                        if cand == os.environ.get("USER", ""):
                            continue
                        cand_db = os.path.join(home_root, cand, ".toolrecall", "cache.db")
                        if os.path.exists(cand_db):
                            other_db = cand_db
                            other_user = cand
                            break
                if not os.path.exists(db_path) and other_db:
                    # Classic multi-user box: the daemon runs as another user
                    # (its state is private to that user), we run as someone
                    # else. Say so instead of a bare 'no data'.
                    y = put(
                        y + 1,
                        "  no cache data for user '%s' — but %s exists (owned by another user)"
                        % (os.environ.get("USER", "?"), other_db),
                        A_MISS,
                    )
                    y = put(
                        y + 1,
                        "  (daemon state is per-user; view it as that user:"
                        " sudo -u %s -i toolrecall top, or copy the db)" % other_user,
                        A_DIM,
                    )
                elif not os.path.exists(db_path):
                    y = put(
                        y + 1,
                        "  no cache data on this machine: %s does not exist" % db_path,
                        A_MISS,
                    )
                    y = put(
                        y + 1,
                        "  (the dashboard shows the machine it runs on — run it where the daemon lives)",
                        A_DIM,
                    )
                else:
                    y = put(
                        y + 1,
                        "  no cache data — daemon down AND db unreadable: %s" % db_path,
                        A_MISS,
                    )
                    y = put(
                        y + 1,
                        "  (check TOOLRECALL_CACHE_DB / start the daemon: toolrecall daemon start)",
                        A_DIM,
                    )
            for cat in CATEGORIES:
                c = st.get(cat)
                if not c:
                    continue
                attr = A_DIM if (c["hits"] + c["misses"]) == 0 else 0
                if narrow:
                    y = put(
                        y,
                        " %-14s %8s %8s %6s %10s"
                        % (
                            cat,
                            f"{c['hits']:,}",
                            f"{c['misses']:,}",
                            c["hit_rate"],
                            human_tokens(c["tokens_served_from_cache"]),
                        ),
                        attr,
                    )
                else:
                    y = put(
                        y,
                        " %-14s %9s %9s %7s %12s %10s"
                        % (
                            cat,
                            f"{c['hits']:,}",
                            f"{c['misses']:,}",
                            c["hit_rate"],
                            human_tokens(c["tokens_served_from_cache"]),
                            age_str(c.get("updated_at", 0)),
                        ),
                        attr,
                    )
            # ── MONEY reconciliation (usage CSV — billed truth) ──
            money = state.get("money")
            if money:
                y = put(y + 1, "", 0)  # section gap
                y = put(y, " money [billed via proxy, today]:", A_BOLD)
                y = put(
                    y,
                    "  requests %d   billed-prompt %s"
                    % (money["requests"], human_tokens(money["prompt"])),
                )
                y = put(
                    y,
                    "  completions %s   replays %d"
                    % (human_tokens(money["completion"]), money["replays"]),
                )
                if money["median"] and money["median"] > 100_000:
                    y = put(
                        y,
                        "  median request %s tok — cost dominated by re-sent history"
                        % human_tokens(money["median"]),
                        A_MISS,
                    )
                    y = put(
                        y,
                        "  → run the checkpoint flow (view 4, press c) to cut history re-billing",
                        A_DIM,
                    )
            else:
                y = put(y + 1, "", 0)  # section gap
                y = put(y, " money [billed]: no proxy traffic captured today", A_DIM)
        elif view == "log":
            rows = state["log"]
            body_h = max(1, max_y - y - 2)
            state["scroll"] = max(0, min(state["scroll"], max(0, len(rows) - body_h)))
            y = put(y, " recent access log (%d rows, scroll up/down):" % len(rows), A_BOLD)
            for row in rows[state["scroll"] : state["scroll"] + body_h]:
                cat, path, hit, tokens, ts = row
                y = put(
                    y,
                    "  %5s  %-13s %-4s %6s  %s"
                    % (
                        age_str(ts),
                        cat,
                        "HIT" if hit else "MISS",
                        human_tokens(tokens) if tokens else "-",
                        path,
                    ),
                    A_HIT if hit else A_MISS,
                )
        elif view == "trend":
            t = state.get("trend")
            y = put(y, " daily hit-rate trend (file_cache, from access_log):", A_BOLD)
            if not t:
                y = put(y + 1, "  trend data unavailable (db unreadable?)", A_DIM)
            else:
                cov = t["coverage"]
                y = put(y + 1, "", 0)  # section gap
                y = put(
                    y,
                    "  coverage: %s → %s" % (cov["oldest"], cov["newest"]),
                    A_DIM,
                )
                y = put(
                    y,
                    "  window: %d/%d entries" % (cov["window_entries"], cov["window_max"]),
                    A_DIM,
                )
                y = put(y + 1, "  date        reads   hits  misses  hit%")
                for d in t["days"]:
                    hp = "-" if d["hit_pct"] is None else "%5.1f" % d["hit_pct"]
                    # crude bar: one ▄ per ~5% — instant visual anchor
                    bar = "▄" * int((d["hit_pct"] or 0) / 5)
                    y = put(
                        y,
                        "  %s  %6d  %6d  %6d  %s  %s"
                        % (d["date"], d["reads"], d["hits"], d["misses"], hp, bar),
                        A_HIT
                        if (d["hit_pct"] or 0) >= 70
                        else (A_MISS if (d["hit_pct"] or 0) < 50 else 0),
                    )
                classes = t["classes"]
                if classes:
                    y = put(y + 1, "", 0)  # section gap
                    y = put(y, "  worst hit-rate classes (min 10 reads):", A_BOLD)
                    for c in classes:
                        # path_class can exceed 42 chars — truncate to tail so
                        # numeric columns stay aligned on narrow terminals.
                        cls = c["path_class"]
                        if len(cls) > 42:
                            cls = "…" + cls[-41:]
                        y = put(
                            y,
                            "  %-42s %6d  %5s%%  %5s"
                            % (cls, c["reads"], c["share_pct"], c["hit_pct"]),
                            A_MISS if (c["hit_pct"] or 100) < 50 else A_DIM,
                        )
        elif view == "context":
            # ── VIEW 1 (cont.): context tracker hygiene — stale + blocked ──
            ctx = state["ctx"]
            y = put(
                y,
                " context tracker (requires the daemon — state lives in daemon memory):",
                A_BOLD,
            )
            if ctx is None:
                y = put(y + 1, "  daemon unreachable — context data unavailable", A_DIM)
                y = put(y + 1, "  start it:  toolrecall daemon start", A_DIM)
            else:
                y = put(
                    y,
                    "  checkpoint %s   dirty %d   clean %d   total read %d"
                    % (
                        ctx.get("checkpoint", "-"),
                        ctx.get("total_dirty", 0),
                        ctx.get("total_clean", 0),
                        ctx.get("total_read", 0),
                    ),
                )
                y = put(y, "", 0)  # section gap
                # "ctx dropped" ledger intentionally NOT rendered for end
                # users (2026-09-16): it is an event-driven, bank-once
                # estimate that reads as stale noise without checkpoint-flow
                # context. End users get the wire-verified measured line;
                # the ledger stays available via `toolrecall stats`.
                # MEASURED tier: wire-verified not-sent tokens (bridge dedup
                # stubs). The number an end user can take to the bank —
                # content was REPLACED on the wire, never an estimate.
                _measured = 0
                for _cat in (state.get("stats") or {}).values():
                    if isinstance(_cat, dict) and _cat.get("tokens_not_sent_measured"):
                        _measured += _cat["tokens_not_sent_measured"]
                if _measured:
                    y = put(
                        y,
                        "  measured not-sent: %s tok  (dedup stubs — wire-verified)"
                        % human_tokens(_measured),
                    )
                stale = ctx.get("stale", [])
                y = put(y + 1, "", 0)  # section gap
                y = put(
                    y,
                    "  stale files (read then overwritten — context copy is"
                    " WRONG): %d  (~%s tok reclaimable)"
                    % (len(stale), human_tokens(ctx.get("est_reclaimable_tokens", 0))),
                    A_BOLD if stale else 0,
                )
                body_h = max(1, max_y - y - 2)
                state["scroll"] = max(0, min(state["scroll"], max(0, len(stale) - body_h)))
                state["sel"] = max(0, min(state["sel"], max(0, len(stale) - 1)))
                for i, e in enumerate(stale[state["scroll"] : state["scroll"] + body_h]):
                    idx = state["scroll"] + i
                    line = "  %s %s (~%s tok, size %s)"
                    args = (
                        ">" if idx == state["sel"] else " ",
                        e["path"],
                        human_tokens(e["est_tokens"]),
                        human_tokens(e.get("size", 0)),
                    )
                    y = put(
                        y,
                        line % args,
                        A_SEL if idx == state["sel"] else (A_MISS if idx == state["sel"] else 0),
                    )
                if stale:
                    y = put(
                        y + 1,
                        "  enter: refresh selected   c: set checkpoint",
                        A_DIM,
                    )
                # Blocked reads FIRST (security info outranks the hygiene list):
                # sensitive/policy-denied content the security gate refused —
                # never sent to the LLM. From the daemon log (today, bounded).
                blocked = blocked_reads_today()
                if blocked:
                    n_blocked = sum(b["count"] for b in blocked)
                    y = put(y + 1, "", 0)  # section gap
                    y = put(
                        y,
                        "  blocked reads (never sent to LLM): %d / %d paths"
                        % (n_blocked, len(blocked)),
                        A_BOLD,
                    )
                    for b in blocked[:4]:
                        y = put(
                            y,
                            "    %4dx  %8s  %s"
                            % (b["count"], b["size_str"], b["path"][: max(24, max_x - 40)]),
                            A_DIM,
                        )
                # Clean files with sizes (the drop candidates behind the
                # ctx-dropped counter — size = what would stop being re-sent).
                clean_paths = ctx.get("clean") or []
                if clean_paths and max_y - y > 4:
                    avail = max_y - y - 2
                    shown = clean_paths[: max(1, avail)]
                    y = put(y + 1, "", 0)  # section gap
                    y = put(
                        y,
                        "  clean files (safe to drop; %d of %d shown):"
                        % (len(shown), len(clean_paths)),
                        A_BOLD,
                    )
                    for p in shown:
                        try:
                            size_str = human_tokens(os.path.getsize(p))
                        except OSError:
                            size_str = "?"
                        y = put(y, "    %8s  %s" % (size_str, p), A_DIM)
        elif view == "stats_footer":
            pass

        # footer: toast or hint
        msg = state["toast"] if time.time() - state["toast_ts"] < 4 else ""
        if not msg:
            msg = {
                "context": "view: saved (billed + measured not-sent) — press 4 for cache IO",
                "stats": "view: per-cache IO stats (tech)",
                "log": "view: access log (up/down scroll)",
                "trend": "view: daily hit-rate trend",
                "help": "view: help",
            }.get(view, "")
        put(max_y - 1, " " + msg, A_DIM)
        scr.refresh()

    while True:
        refresh_data()
        draw()
        try:
            key = scr.getch()
        except curses.error:
            key = -1
        if key == 27:
            # ESC starts an escape sequence (arrow keys etc.). Peek: if no
            # follow-up byte arrives within 40ms, treat lone ESC as quit.
            scr.timeout(40)
            try:
                k2 = scr.getch()
            except curses.error:
                k2 = -1
            scr.nodelay(True)
            if k2 == -1:
                return 0  # lone ESC → quit
            if k2 in (ord("["), ord("O")):
                try:
                    k3 = scr.getch()  # final byte: A/B/C/D
                except curses.error:
                    k3 = -1
                if k3 == ord("A"):
                    key = curses.KEY_UP
                elif k3 == ord("B"):
                    key = curses.KEY_DOWN
                elif k3 == ord("C"):
                    key = curses.KEY_RIGHT
                elif k3 == ord("D"):
                    key = curses.KEY_LEFT
                else:
                    key = -1
            else:
                key = -1
        if key == ord("q"):
            return 0
        elif key in (ord("1"), ord("2"), ord("3"), ord("4"), ord("5")):
            state["view"] = VIEW_NAMES[key - ord("1")]
            state["sel"] = 0
            state["scroll"] = 0
        elif key == 9:  # tab
            i = VIEW_NAMES.index(state["view"])
            state["view"] = VIEW_NAMES[(i + 1) % len(VIEW_NAMES)]
            state["sel"] = 0
            state["scroll"] = 0
        elif key in (curses.KEY_DOWN, ord("j")):
            state["sel"] += 1
            state["scroll"] += 1 if state["sel"] > state["scroll"] else 0
        elif key in (curses.KEY_UP, ord("k")):
            state["sel"] = max(0, state["sel"] - 1)
            state["scroll"] = max(0, min(state["scroll"], state["sel"]))
        elif key == curses.KEY_PPAGE:
            state["scroll"] = max(0, state["scroll"] - 10)
        elif key == curses.KEY_NPAGE:
            state["scroll"] += 10
        elif key in (ord("r"), curses.KEY_REFRESH):
            refresh_data(force=True)
        elif key == ord("c") and state["view"] == "context":
            ok, msg = action_set_checkpoint()
            toast(("OK: " if ok else "ERR: ") + msg)
        elif key in (10, 13, curses.KEY_ENTER) and state["view"] == "context":
            ctx = state["ctx"] or {}
            stale = ctx.get("stale", [])
            if stale and 0 <= state["sel"] < len(stale):
                path = stale[state["sel"]]["path"]
                ok, msg = action_refresh_file(path)
                toast(("OK: " if ok else "ERR: ") + msg)
                refresh_data(force=True)
            else:
                toast("no stale file selected")
        time.sleep(min(0.3, max(0.05, interval / 4)))


def main(argv: list | None = None) -> int:
    ap = argparse.ArgumentParser(prog="toolrecall top", description=__doc__)
    ap.add_argument("--interval", type=float, default=2.0, help="refresh seconds")
    ap.add_argument("--db", default=None, help="cache.db path override")
    ap.add_argument("--once", action="store_true", help="one snapshot, no curses")
    args = ap.parse_args(argv)

    db = resolve_db(args.db)
    snapshot = lambda: snapshot_factory(db)  # noqa: E731
    log_fn = lambda: access_rows(db)  # noqa: E731

    if args.once or not sys.stdout.isatty():
        snap = snapshot()
        snap["db"] = db
        print(render_plain(snap))
        return 0

    try:
        return curses.wrapper(run_curses, snapshot, log_fn, args.interval)
    except KeyboardInterrupt:
        # Ctrl+C: terminal already restored by curses.wrapper's finally.
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
