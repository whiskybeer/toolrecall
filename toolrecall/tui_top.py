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

# Context wall for the session-replay tier: naive requests above this many
# tokens cannot exist (the provider rejects/overflows them — a real-world
# run hits compaction or a fresh session instead). NOT the UI's model limit;
# the constant that separates "re-sent history the provider would refuse"
# from "expensive but billable". Disclosed in HELP_TEXT; tuning it changes
# only the wall-crossing count, never the measured token sums.
_CONTEXT_WALL = 200_000

HELP_TEXT = [
    "toolrecall top — interactive dashboard (read-only)",
    "",
    "1/2/3/4   switch view: saved (savings) / access log / trend / stats",
    "tab       cycle views",
    "up/down   scroll (log, context views); move selection",
    "enter     context view: refresh the selected stale file on disk",
    "c         context view: set a new checkpoint (context tracker)",
    "r         force refresh now",
    "q / esc   quit",
    "",
    "SAVED VIEW (press 1): how much was NOT sent to the model?",
    "  headline      tokens never sent, as a multiple of what was sent",
    "                and in whole context windows.",
    "  comparison    sent vs what the model would have received without",
    "                ToolRecall (same sessions, recalculated from today's",
    "                actual requests).",
    "  big message   the largest single message, and how big it would",
    "                have been — the same removed content piled up, not",
    "                an extra saving. Shown only when it differs.",
    "  the limit is MEASURED: the largest request the provider actually",
    "  accepted (a hard-coded 200K was empirically wrong).",
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
            "SELECT category, hits, misses, io_tokens_saved,"
            " tokens_saved_to_llm, updated_at FROM cache_stats"
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
      — the per-request Y in "sent X, not sent Y"),
      cache_read_tokens (provider prefix-cache reads — v4 rows carry the
      OpenAI prompt_tokens_details.cached_tokens nesting; 0 on pre-v4 rows),
      cache_discount_usd (provider-reported cache discount in dollars —
      0 = not reported, never guessed).
    """
    path = usage_csv or os.path.join(os.path.expanduser("~"), ".toolrecall", "proxy_usage.csv")
    if not os.path.isfile(path):
        return None
    import csv as _csv
    from datetime import datetime, timezone

    requests = prompt = completion = replays = 0
    sizes = []
    dedup_tokens = 0
    cache_read = 0
    cache_discount = 0.0
    cost = 0.0
    cost_rows = 0
    replay_tokens = 0
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
                # A HIT is a call ToolRecall answered from cache — the model
                # never saw it. Its prompt tokens are NOT sent; counting them
                # in `prompt` put ToolRecall's single largest saving on the
                # cost side of the ledger (2026-09-21: 7,289 lifetime HITs,
                # 39.1M tokens, all mis-signed). Split before summing.
                if (row.get("cache_status") or "").strip().upper() == "HIT":
                    replays += 1
                    replay_tokens += p
                    # Cost on a HIT row is the cached body's ORIGINAL price
                    # replayed, not money charged again — the provider was
                    # never called. Skipping it keeps `cost_usd` billed-truth.
                    continue
                prompt += p
                completion += int(row.get("completion_tokens") or 0)
                if p > 0:
                    sizes.append(p)
                try:
                    dedup_tokens += int(row.get("dedup_tokens") or 0)
                except (TypeError, ValueError):
                    pass
                try:
                    cache_read += int(row.get("cache_read_tokens") or 0)
                except (TypeError, ValueError):
                    pass
                try:
                    cache_discount += float(row.get("cache_discount_usd") or 0.0)
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
        # Prompt tokens of calls answered entirely from cache — the model was
        # never asked, so these are NOT sent (see the HIT split above).
        "replay_tokens": replay_tokens,
        # Provider prefix-cache reads (cache_read_tokens column; v4 rows carry
        # the OpenAI prompt_tokens_details.cached_tokens nesting) and the
        # provider-reported cache discount in dollars (v4; 0 = not reported —
        # never claim a dollar saving the provider didn't report).
        "cache_read_tokens": cache_read,
        "cache_discount_usd": cache_discount,
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


def session_replay_today(usage_csv: str | None = None, db: str | None = None) -> dict | None:
    """Naive-equivalent context load via REPLAY (deterministic, wire-measured).

    The umbrella counts tokens REMOVED at the stub event once — but in a
    naive world (no ToolRecall) that removed content would ride along in
    EVERY subsequent request of the conversation (prompt = full history).
    The session-length claim (7-9x) lives in that amplification, which no
    per-event counter can see. This collector reconstructs it:

      - Sessions: CSV requests (non-HIT) split at >15 min gaps — the same
        gap rule attaches a stub to the next request in its session.
      - Actual request size  = billed prompt p (what the provider saw).
      - Naive request size   = p + dedup_tokens (blocks TR removed within
        the request) + Σ stub tokens stubbed earlier in the session (the
        content a naive world would re-send every turn).
      - Wall = _CONTEXT_WALL tokens: naive requests above it are requests
        the naive world CANNOT make (context overflow → compaction or a
        new session). That count IS the session-capacity story.

    Every input is measured (billed prompts, wire-verified stubs, logged
    timestamps); the construction is the replay: one deterministic
    re-computation with the stubs/dedup switched off. Estimates here are
    the segmentation choices (15-min gap, wall constant), disclosed in
    HELP_TEXT — never a hidden multiplier.

    Returns None when there is no traffic today. HIT rows are excluded
    from the request stream (their prompt column carries the synthesized
    response size, not a request prompt).
    """
    path = usage_csv or os.path.join(os.path.expanduser("~"), ".toolrecall", "proxy_usage.csv")
    if not os.path.isfile(path):
        return None
    import bisect
    import csv as _csv
    from datetime import datetime, timezone

    day_start = (
        datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    )
    reqs: list[tuple[float, int, int]] = []
    try:
        with open(path, errors="replace") as f:
            for row in _csv.DictReader(f):
                try:
                    ts = float(row.get("timestamp") or 0)
                except ValueError:
                    continue
                if ts < day_start:
                    continue
                if (row.get("cache_status") or "").strip().upper() == "HIT":
                    continue  # replay rows carry no request prompt
                try:
                    p = int(float(row.get("prompt_tokens") or 0))
                    d = int(float(row.get("dedup_tokens") or 0))
                except (TypeError, ValueError):
                    continue
                reqs.append((ts, p, d))
    except OSError:
        return None
    if not reqs:
        return None
    reqs.sort()

    # Wire-verified stub events (tokens, timestamp) from access_log.
    stubs: list[tuple[float, int]] = []
    con = _open_ro(db) if db else None
    if con is not None:
        try:
            for ts, tokens in con.execute(
                "SELECT cached_at, tokens FROM access_log"
                " WHERE category = 'mcp_dedup' AND cached_at >= ? AND tokens > 0",
                (day_start,),
            ):
                stubs.append((float(ts), int(tokens or 0)))
        except sqlite3.Error:
            stubs = []  # unreadable log → replay tier degrades to None downstream
        finally:
            con.close()
    stubs.sort()

    # Attach each stub to its session: the next request within GAP seconds.
    GAP = 900  # 15 min idle = session boundary (same rule for both uses)
    rts = [t for t, _, _ in reqs]
    attached: list[tuple[float, int]] = []
    for ts, tk in stubs:
        i = bisect.bisect_right(rts, ts)
        if i < len(rts) and rts[i] - ts < GAP:
            attached.append((ts, tk))
    attached.sort()

    # Split sessions at the same gap.
    sessions: list[list[tuple[float, int, int]]] = []
    cur = [reqs[0]]
    for prev, nxt in zip(reqs, reqs[1:]):
        if nxt[0] - prev[0] > GAP:
            sessions.append(cur)
            cur = []
        cur.append(nxt)
    sessions.append(cur)

    act_total = naive_total = act_peak = naive_peak = 0
    dedup_sum = 0
    ride_slots = 0  # measured: requests that carry at least one removed part
    # Pass 1: totals and peaks (the limit is only knowable after the peak).
    for s in sessions:
        s_first, s_last = s[0][0], s[-1][0]
        s_stubs = [t for t in attached if s_first <= t[0] <= s_last]
        out = 0
        si = 0
        for ts, p, d in s:
            while si < len(s_stubs) and s_stubs[si][0] <= ts:
                out += s_stubs[si][1]
                si += 1
            a = p
            n = p + d + out
            dedup_sum += d
            if out:
                ride_slots += 1
            act_total += a
            naive_total += n
            act_peak = max(act_peak, a)
            naive_peak = max(naive_peak, n)
    if naive_total <= 0:
        return None
    # MEASURED context limit (2026-09-20): the old hard-coded 200K was
    # empirically wrong — a 245,947-token request was accepted and billed,
    # so the view claimed "over the limit" for requests the provider took.
    # The largest ACTUAL request proves the model accepts at least that
    # much; the floor keeps a sane value on tiny days. Only naive requests
    # above this proven-acceptable size count as impossible.
    limit = max(_CONTEXT_WALL, act_peak)
    # Pass 2: limit crossings against the measured limit.
    wall_naive = wall_act = 0
    for s in sessions:
        s_first, s_last = s[0][0], s[-1][0]
        s_stubs = [t for t in attached if s_first <= t[0] <= s_last]
        out = 0
        si = 0
        for ts, p, d in s:
            while si < len(s_stubs) and s_stubs[si][0] <= ts:
                out += s_stubs[si][1]
                si += 1
            if p + d + out > limit:
                wall_naive += 1
            if p > limit:
                wall_act += 1
    attached_tokens = sum(tk for _, tk in attached)
    # Bridge arithmetic (what the saved view explains): naive = actual +
    # avoided. avoided splits into the request's own deduped blocks (counted
    # once — conservative: a naive client would re-send duplicates in later
    # turns too, so this UNDERcounts) and the attached stub tokens riding
    # along in every later request of their session (the amplification).
    amplified_stubs = naive_total - act_total - dedup_sum
    return {
        "requests": len(reqs),
        "sessions": len(sessions),
        "stubs_attached": len(attached),
        "stubs_dropped": len(stubs) - len(attached),
        "actual_total": act_total,
        "naive_total": naive_total,
        "avoided_total": naive_total - act_total,
        "dedup_sum": dedup_sum,
        "stub_attached_tokens": attached_tokens,
        "amp_stubs": (amplified_stubs / attached_tokens) if attached_tokens else 0.0,
        "ride_slots": ride_slots,
        "savings_pct": 100.0 * (1.0 - act_total / naive_total),
        "amp": naive_total / max(1, act_total),
        "peak_actual": act_peak,
        "peak_naive": naive_peak,
        "wall_naive": wall_naive,
        "wall_act": wall_act,
        "limit": limit,
    }


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
        % ("cache [IO]", "hits", "misses", "rate", "IO saved", "last-act")
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
    # 2026-09-19 layout: the saved view = three sections, one question each
    # (1 held back / 2 session capacity / 3 billed) — HELP_TEXT documents it.
    money = money_today()
    stubs_today, stubs_window = stubbed_today(snapshot.get("db_path") or snapshot.get("db", ""))
    _dy = (money or {}).get("dedup_tokens", 0)
    _notsent = _dy + stubs_today

    lines.append("")
    # ── HEADLINE: not sent to the model (2026-09-20: this is where the
    # saving is — Robin's call). Bill passage and today-footer removed.
    _rp = snapshot.get("replay") or {}
    _avoided = _rp.get("avoided_total", _notsent)
    _naive = _rp.get("naive_total", (money or {}).get("prompt", 0) + _notsent)
    _sent = (money or {}).get("prompt", 0)
    lines.append("SAVED TO LLM TODAY:  %s tok  (never sent to the model)" % human_tokens(_avoided))
    if _sent and _avoided:
        _ratio = _avoided / _sent
        _windows = _avoided / float(_rp.get("limit") or _CONTEXT_WALL)
        lines.append(
            "    %.2fx everything that was sent  ·  ~ %.1f full context windows"
            % (_ratio, _windows)
        )
    lines.append("")
    if money:
        lines.append("  Sent to the model:   %s tok" % human_tokens(_sent))
        _more = (_naive / _sent) if _sent else 0.0
        lines.append(
            "  Without ToolRecall:  %s tok   —  %.2fx more for the model to read"
            % (human_tokens(_naive), _more)
        )
    # Biggest single message: only when the removed content actually piled
    # up again (peak_naive > peak_actual); otherwise the line says nothing.
    if _rp.get("peak_naive", 0) > _rp.get("peak_actual", 0):
        _pa, _pn = _rp["peak_actual"], _rp["peak_naive"]
        lines.append("")
        lines.append(
            "  Biggest single message:  %s tok  ->  without ToolRecall %s tok  (%.1fx)"
            % (human_tokens(_pa), human_tokens(_pn), _pn / max(1, _pa))
        )
        lines.append("    same removed content piled up again in one message — not an extra saving")
    if _rp.get("wall_naive", 0) > _rp.get("wall_act", 0):
        lines.append(
            "    %d of %d messages would not have fit at all"
            " (largest accepted: %s tok)"
            % (_rp["wall_naive"], _rp["requests"], human_tokens(_rp.get("limit", 0)))
        )
    lines.append("")
    if _notsent:
        lines.append("  Why: your agent reads the same files again, and every message repeats")
        lines.append("  the whole conversation. ToolRecall answers a re-read from memory, so")
        lines.append("  that content is sent once instead of in every later message.")
    else:
        lines.append("  Nothing was held back today (no repeated file reads, no repeated text).")
    lines.append("")
    if money:
        _cost = ""
        if money.get("cost_usd") is not None:
            _cost = "  $%.4f" % money["cost_usd"]
        lines.append(
            "Details: %s prompt / %s completion tok billed%s  ·  replays %d"
            % (
                human_tokens(money["prompt"]),
                human_tokens(money["completion"]),
                _cost,
                money["replays"],
            )
        )
        _cr = money.get("cache_read_tokens") or 0
        _cd = money.get("cache_discount_usd") or 0.0
        if _cr or _cd:
            # Provider prefix-cache: a BILL-side discount on SENT tokens —
            # deliberately NOT in the not-sent math.
            _cpct = (100.0 * _cr / money["prompt"]) if money["prompt"] else 0.0
            _line = "  provider prefix-cache: %s tok re-served by provider  —  %.1f%% of sent" % (
                human_tokens(_cr),
                _cpct,
            )
            if _cd:
                _line += "  ·  discount $%.4f (provider-reported)" % _cd
            lines.append(_line)
    else:
        lines.append("  no proxy traffic captured today")
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
        "replay": None,
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
        # session-replay tier (naive-world reconstruction) — same cadence
        try:
            state["replay"] = session_replay_today(db=(state["snap"] or {}).get("db_path", ""))
        except Exception:  # noqa: BLE001 — replay block must never break the TUI
            state["replay"] = None
        # trend reads the db directly (like log_fn); cheap + daemon-independent
        try:
            from toolrecall.trend import coverage, daily_trend, miss_classes, savings_trend

            state["trend"] = {
                "days": daily_trend(7),
                "mcp": daily_trend(7, "mcp_cache"),
                "classes": miss_classes(min_reads=10),
                "coverage": coverage(),
                "savings": savings_trend(7),
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
            # ── VIEW 1: SAVED — not-sent headline, plain words ──
            ctx = state["ctx"]
            money = state.get("money")
            _dy = (money or {}).get("dedup_tokens", 0)
            _st = state.get("stubs_today", 0) or 0
            _notsent = _dy + _st
            _rp = state.get("replay") or {}
            _avoided = _rp.get("avoided_total", _notsent)
            _naive = _rp.get("naive_total", (money or {}).get("prompt", 0) + _notsent)
            _sent = (money or {}).get("prompt", 0)

            y = put(y, "", 0)
            y = put(
                y,
                " SAVED TO LLM TODAY:  %s tok  (never sent to the model)" % human_tokens(_avoided),
                A_BOLD,
            )
            if _sent and _avoided:
                y = put(
                    y,
                    "     %.2fx everything that was sent  ·  ~ %.1f full context windows"
                    % (_avoided / _sent, _avoided / float(_rp.get("limit") or _CONTEXT_WALL)),
                    A_HIT,
                )
            y = put(y, "", 0)
            if money:
                y = put(y, "   Sent to the model:   %s tok" % human_tokens(_sent))
                y = put(
                    y,
                    "   Without ToolRecall:  %s tok   —  %.2fx more for the model to read"
                    % (human_tokens(_naive), (_naive / _sent) if _sent else 0.0),
                )
            if _rp.get("peak_naive", 0) > _rp.get("peak_actual", 0):
                _pa, _pn = _rp["peak_actual"], _rp["peak_naive"]
                y = put(y, "", 0)
                y = put(
                    y,
                    "   Biggest single message:  %s tok  ->  without ToolRecall %s tok  (%.1fx)"
                    % (human_tokens(_pa), human_tokens(_pn), _pn / max(1, _pa)),
                    A_BOLD,
                )
                y = put(
                    y,
                    "     same removed content piled up again in one message — not an extra saving",
                )
            if _rp.get("wall_naive", 0) > _rp.get("wall_act", 0):
                y = put(
                    y,
                    "     %d of %d messages would not have fit at all (largest accepted: %s tok)"
                    % (
                        _rp["wall_naive"],
                        _rp["requests"],
                        human_tokens(_rp.get("limit", 0)),
                    ),
                    A_HIT,
                )
            y = put(y, "", 0)
            if _notsent:
                y = put(
                    y,
                    "   Why: your agent reads the same files again, and every message",
                )
                y = put(
                    y,
                    "   repeats the whole conversation. ToolRecall answers a re-read from",
                )
                y = put(
                    y,
                    "   memory, so that content is sent once instead of in every later one.",
                )
            else:
                y = put(
                    y,
                    "   Nothing was held back today (no repeated file reads, no repeated text).",
                    A_DIM,
                )
            y = put(y, "", 0)
            if money:
                _cost = ""
                if money.get("cost_usd") is not None:
                    _cost = "  $%.4f" % money["cost_usd"]
                y = put(
                    y,
                    " Details: %s prompt / %s completion tok billed%s  ·  replays %d"
                    % (
                        human_tokens(money["prompt"]),
                        human_tokens(money["completion"]),
                        _cost,
                        money["replays"],
                    ),
                )
                _cr = money.get("cache_read_tokens") or 0
                _cd = money.get("cache_discount_usd") or 0.0
                if _cr or _cd:
                    _cpct = (100.0 * _cr / money["prompt"]) if money["prompt"] else 0.0
                    _line = (
                        "  provider prefix-cache: %s tok re-served by provider  —  %.1f%% of sent"
                        % (human_tokens(_cr), _cpct)
                    )
                    if _cd:
                        _line += "  ·  discount $%.4f (provider-reported)" % _cd
                    y = put(y, _line, A_HIT)
            else:
                y = put(y, " Details: no proxy traffic captured today", A_DIM)
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
                    "  IO saved (disk reads avoided, not money): %s" % human_tokens(_io_saved),
                    A_DIM,
                )
        elif view == "stats":
            # ── VIEW 4: per-cache IO stats (tech view) ──
            narrow = max_x < 88
            if narrow:
                # phone/narrow SSH: drop the last-act column (low value)
                y = put(
                    y,
                    " %-14s %8s %8s %6s %10s" % ("cache [IO]", "hits", "miss", "rate", "IO saved"),
                    A_BOLD,
                )
            else:
                y = put(
                    y,
                    " %-14s %9s %9s %7s %12s %10s"
                    % ("cache [IO]", "hits", "misses", "rate", "IO saved", "last-act"),
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
            y = put(y, " daily savings trend (7d, from the usage log):", A_BOLD)
            if not t:
                y = put(y + 1, "  trend data unavailable (db unreadable?)", A_DIM)
            else:
                # ── LLM savings (append-only usage CSV: real history) ──
                y = put(y + 1, "", 0)  # section gap
                y = put(
                    y,
                    "  date          calls          sent      not sent   saved     cost",
                    A_DIM,
                )
                for s in t.get("savings") or []:
                    pct = "%5.1f%%" % s["saved_pct"] if s["saved_pct"] is not None else "    -"
                    y = put(
                        y,
                        "  %s  %6s  %12s  %12s  %s  $%6.2f"
                        % (
                            s["date"],
                            f"{s['calls']:,}",
                            f"{s['sent']:,}",
                            f"{s['not_sent']:,}",
                            pct,
                            s["cost_usd"],
                        ),
                        A_HIT if (s["saved_pct"] or 0) >= 1.0 else 0,
                    )
                if not (t.get("savings") or []):
                    y = put(
                        y,
                        "  no model calls logged yet — this fills in as your agent works",
                        A_DIM,
                    )
                y = put(y + 1, "", 0)  # section gap

                # ── Disk I/O hit rate (rolling 50k access_log window) ──
                cov = t["coverage"]
                y = put(
                    y,
                    "  disk reads served from cache (I/O, not tokens):",
                    A_BOLD,
                )
                y = put(
                    y,
                    "  coverage: %s → %s  (%d/%d entries)"
                    % (cov["oldest"], cov["newest"], cov["window_entries"], cov["window_max"]),
                    A_DIM,
                )
                if cov.get("infra_entries"):
                    y = put(
                        y,
                        "  (%s agent-infrastructure reads excluded — never sent to a model)"
                        % f"{cov['infra_entries']:,}",
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
                # ── MCP replays (mcp_cache) ────────────────────────────────
                # Its own section on purpose: these replay MCP server responses
                # (synthetic mcp:<server>/<tool> paths), they are not disk
                # reads, and their saving is I/O only — the body still reaches
                # the model, so folding them into the file table above would
                # claim a token saving that does not exist.
                mcp_days = t.get("mcp") or []
                if mcp_days:
                    y = put(y + 1, "", 0)  # section gap
                    y = put(y, "  MCP server replays (I/O, not tokens):", A_BOLD)
                    y = put(y, "  date       replays   hits   hit%", A_DIM)
                    for d in mcp_days:
                        hp = "-" if d["hit_pct"] is None else "%5.1f" % d["hit_pct"]
                        y = put(
                            y,
                            "  %s  %7d  %6d  %s" % (d["date"], d["reads"], d["hits"], hp),
                            A_HIT if (d["hit_pct"] or 0) >= 70 else 0,
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
        try:
            snap["replay"] = session_replay_today(db=db)
        except Exception:  # noqa: BLE001 — replay must never break the snapshot
            snap["replay"] = None
        print(render_plain(snap))
        return 0

    try:
        return curses.wrapper(run_curses, snapshot, log_fn, args.interval)
    except KeyboardInterrupt:
        # Ctrl+C: terminal already restored by curses.wrapper's finally.
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
