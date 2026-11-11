"""
toolrecall.shim — Transparent OS-level file-read cache shim.

Installation (one-time):
    toolrecall shim --install

This creates a .pth file in site-packages that auto-patches `open()`
(read-only) on every Python process startup. Zero imports needed from the
calling code — any Python process (Hermes, Codex, Aider, Cursor, scripts)
transparently benefits from file-read caching.

Scope (Option B — see CHANGELOG v0.8.19):
    Patches ONLY `builtins.open` for read (`r`/`rt`) mode. It does NOT
    intercept `subprocess.run()`/`subprocess.Popen()`. Terminal command
    output is NOT transparently cached at the shim layer: doing so is
    fundamentally lossy because it runs the command in the daemon's working
    directory and environment rather than the calling agent's, then replays
    the (wrong) output. That wrong-cwd class of bug surfaced as garbled
    shell output and offline-looking sessions. Terminal/output caching that
    is known to be read-only and deterministic stays available explicitly
    via the daemon (`cached_terminal`), never through the implicit shim.

File-read caching is safe to shim because it is keyed on path + mtime +
size — a read is deterministic given the same bytes, so a cache hit can
never return wrong content.

Uninstall:
    toolrecall shim --uninstall

Config:
    TOOLRECALL_SHIM_DISABLE=1  — disable shim at runtime
    toolrecall shim --disable  — persistent disable (writes ~/.toolrecall/shim.disabled)
    toolrecall shim --enable   — clear the persistent disable marker
"""

import os
import builtins
import logging
import sys
import threading
import time
from collections import OrderedDict


def _marker_path() -> str:
    """Path of the persistent disable marker.

    Honors ``TOOLRECALL_SHIM_MARKER`` to point at a custom location (useful for
    tests and multi-daemon setups). Defaults to ~/.toolrecall/shim.disabled.
    """
    override = os.environ.get("TOOLRECALL_SHIM_MARKER", "")
    if override:
        return override
    return os.path.join(os.path.expanduser("~/.toolrecall"), "shim.disabled")


def _marker_disabled() -> bool:
    """True if a persistent disable marker file exists."""
    p = _marker_path()
    if not p:
        return False
    return os.path.isfile(p)


_ENABLED = not (os.environ.get("TOOLRECALL_SHIM_DISABLE", "") or _marker_disabled())

# ─── Unchanged-file stub (Lever 1, 2026-09-17) ──────────────────────────────
# Second+ native read of an UNCHANGED file in an agent process -> serve a
# one-line stub instead of full content. Wire-verified not-sent (the content
# provably never reaches the LLM), counted in the same mcp_dedup measured
# tier as the bridge stub. Contract + safety rails live in shim_stub.py.
try:
    from toolrecall.shim_stub import (  # noqa: E402
        maybe_stub as _stub_maybe,
        reset_path as _stub_reset_path,
        reset_all as _stub_reset_all,
    )
except Exception:  # noqa: BLE001 — stub is an enhancement, never a gate
    _stub_maybe = None  # type: ignore[assignment]
    _stub_reset_path = None  # type: ignore[assignment]
    _stub_reset_all = None  # type: ignore[assignment]

# ─── Re-entrancy guard ───
# Prevents infinite recursion when the shim's own code path (importing
# client, connecting to daemon, reading cache DB) calls open() — which
# would be patched and call back into the shim.
# Each thread gets its own guard so concurrent Python processes are
# not blocked by each other.
_thread_local = threading.local()


def _shim_active() -> bool:
    """Check if this thread is already inside a shimmed open() call."""
    return getattr(_thread_local, "active", False)


def _enter_shim():
    """Mark thread as inside shim scope. Returns previous state."""
    prev = getattr(_thread_local, "active", False)
    _thread_local.active = True
    return prev


def _exit_shim(prev: bool):
    """Restore thread's shim-active state to what it was before entry."""
    _thread_local.active = prev


# ─── Lazy-load client on first call ───
_TR = None


def _get_tr():
    global _TR
    if _TR is None and _ENABLED:
        try:
            # Use relative import so the client module is loaded from
            # the same package directory as this shim module — not from
            # wherever sys.path resolves "toolrecall" (which can be the
            # source tree if an editable install shadows site-packages).
            from .client import cached_read as cr, send_cmd as sc

            _TR = {"read": cr, "send": sc}
        except ImportError:
            _TR = False
    return _TR


# ─── Read-path budget + circuit breaker ───────────────────────────────────
# cached_read runs INSIDE the caller's open(). transport.Client defaults to a
# 5 s timeout, so a busy daemon charged every read 5 s (19 WebUI requests
# stalled 5.5-5.8 s in 7 days, thread stack in recv_exact) and, with no breaker,
# charged the next read again. The read path gets its own short budget; after
# _BREAKER_THRESHOLD consecutive timeouts the shim stops talking to the daemon
# for _BREAKER_COOLDOWN_S and serves the real open() instead.
_READ_TIMEOUT_DEFAULT_MS = 500  # >10x the p99 local shim read on this class of box
_BREAKER_THRESHOLD = 3
_BREAKER_COOLDOWN_S = 30.0

_breaker_lock = threading.Lock()
_breaker = {"timeouts": 0, "open_until": 0.0, "recovering": False}


def _read_timeout_s() -> float:
    """Read-path socket budget in seconds (env TOOLRECALL_SHIM_READ_TIMEOUT_MS)."""
    raw = (os.environ.get("TOOLRECALL_SHIM_READ_TIMEOUT_MS") or "").strip()
    if raw:
        try:
            ms = float(raw)
            if ms > 0:
                return ms / 1000.0
        except ValueError:
            logging.getLogger("toolrecall.shim").debug(
                "TOOLRECALL_SHIM_READ_TIMEOUT_MS=%r is not a number", raw
            )
    return _READ_TIMEOUT_DEFAULT_MS / 1000.0


def _breaker_allows_read() -> bool:
    """False → skip the daemon entirely and read from disk.

    Half-open: when the cooldown expires the next read is let through to probe
    the daemon; if it times out the breaker re-opens immediately (threshold 1
    while recovering) instead of paying the full threshold again.
    """
    with _breaker_lock:
        if not _breaker["open_until"]:
            return True
        if time.monotonic() < _breaker["open_until"]:
            return False
        _breaker["open_until"] = 0.0
        _breaker["recovering"] = True
        return True


def _note_read_timeout(path: str) -> None:
    with _breaker_lock:
        _breaker["timeouts"] += 1
        threshold = 1 if _breaker["recovering"] else _BREAKER_THRESHOLD
        if _breaker["timeouts"] >= threshold:
            _breaker["timeouts"] = 0
            _breaker["recovering"] = False
            _breaker["open_until"] = time.monotonic() + _BREAKER_COOLDOWN_S
    logging.getLogger("toolrecall.shim").warning(
        "daemon read timed out; served from disk: %s", path
    )
    try:
        from toolrecall.shim_timeouts import record as _record_timeout

        _record_timeout(path)
    except Exception:  # noqa: BLE001 — accounting must never break the read
        logging.getLogger("toolrecall.shim").debug("shim timeout accounting failed", exc_info=True)


def _note_read_ok() -> None:
    with _breaker_lock:
        if _breaker["timeouts"] or _breaker["recovering"] or _breaker["open_until"]:
            _breaker["timeouts"] = 0
            _breaker["recovering"] = False
            _breaker["open_until"] = 0.0


# ─── W2: in-process local-hit cache ─────────────────────────────────────────
# A warm hit used to cost a UDS round trip (~1.6 ms p50) — ~79x the open() it
# replaces. The entry lives in THIS process, keyed on the same identity the
# daemon uses (realpath, mtime_ns, size, ino): any change to the file changes
# the key, so a stale entry cannot be served. The stat IS the gate.
#
# Materialised temp files are KEPT per entry, not unlinked per call: the write
# half of materialisation was 0.147 ms of every hit. Reopening a kept file is
# the remaining cost. Callers already received a temp path as ``.name``, so
# nothing observable changes; entries are unlinked on evict and at exit, and a
# hard kill can leave at most 128 files (mkstemp 0600) behind.
#
# Accounting: a local hit is a real serve the daemon never sees. Entries are
# queued and flushed in batches (32 hits or 2 s) plus atexit — batched rather
# than per-hit so the fast path stays fast, and flushed at all so a faster
# cache cannot make the savings counters smaller. A daemon that does not know
# the command drops the batch; accounting never breaks a read.
_LOCAL_MAX_ENTRIES = 128
_LOCAL_MAX_BYTES = 32 * 1024 * 1024
_LOCAL_FLUSH_AT = 32
_LOCAL_FLUSH_DELAY_S = 2.0
_LOCAL_TMP_PREFIX = ".toolrecall-localhit-"

_local_lock = threading.RLock()
_local_entries: "OrderedDict[tuple, dict]" = OrderedDict()
_local_nbytes = 0
_local_pending: list = []
_local_timer = None
_local_exit_hooked = False
_local_agent_id = None
_local_agent_id_done = False
# Capability flag: set False if the daemon does not know ``local_hits``.
# Continuing on the fast path would make the hit counters silently skip every
# local hit, so an old daemon downgrades this process to the IPC path instead.
_local_supported = True


def _local_enabled() -> bool:
    if not _local_supported:
        return False
    raw = (os.environ.get("TOOLRECALL_SHIM_LOCAL") or "").strip().lower()
    return raw not in ("0", "false", "no", "off")


def _local_key(path_str: str):
    """(identity key, stat) for a path — the invalidation gate."""
    st = os.stat(path_str)
    return (os.path.realpath(path_str), st.st_mtime_ns, st.st_size, st.st_ino), st


def _local_lookup(path_str: str):
    if not _local_enabled():
        return None
    try:
        key, _st = _local_key(path_str)
    except OSError:
        return None
    with _local_lock:
        entry = _local_entries.get(key)
        if entry is None:
            return None
        _local_entries.move_to_end(key)
    return entry


def _local_materialise(content: str):
    """Write ``content`` to a kept temp file (utf-8, newline='' preserved)."""
    import tempfile

    fd = None
    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(prefix=_LOCAL_TMP_PREFIX, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(content)
        fd = None
        return tmp
    except Exception:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass
        return None


def _local_unlink(entry) -> None:
    if not isinstance(entry, dict):
        return
    for field in ("tmp", "stub_tmp"):
        tmp = entry.get(field)
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def _local_exit() -> None:
    try:
        _local_flush_pending()
    except Exception:
        pass
    try:
        local_cache_reset()
    except Exception:
        pass


def _local_hook_exit() -> None:
    global _local_exit_hooked
    if _local_exit_hooked:
        return
    _local_exit_hooked = True
    try:
        import atexit

        atexit.register(_local_exit)
    except Exception:
        pass


def local_cache_reset() -> None:
    """Drop every in-process entry and queued counter (tests, reset paths)."""
    global _local_nbytes, _local_timer
    with _local_lock:
        timer, _local_timer = _local_timer, None
    if timer is not None:
        try:
            timer.cancel()
        except Exception:
            pass
    with _local_lock:
        entries = list(_local_entries.values())
        _local_entries.clear()
        _local_nbytes = 0
        _local_pending.clear()
    for entry in entries:
        _local_unlink(entry)


def _local_agent():
    """Agent identity for per-agent attribution, resolved once per process."""
    global _local_agent_id, _local_agent_id_done
    if _local_agent_id_done:
        return _local_agent_id
    _local_agent_id_done = True
    try:
        from .client import _resolve_agent_id

        _local_agent_id = _resolve_agent_id(None)
    except Exception:
        _local_agent_id = None
    return _local_agent_id


def _local_store(path_str: str, content: str, lossless: bool, source: str, st=None):
    """Insert/refresh the entry for ``path_str``. Returns the entry or None."""
    global _local_nbytes
    if not _local_enabled() or not isinstance(content, str):
        return None
    try:
        if st is None:
            key, st = _local_key(path_str)
        else:
            key = (os.path.realpath(path_str), st.st_mtime_ns, st.st_size, st.st_ino)
    except OSError:
        return None
    tmp = _local_materialise(content)
    if tmp is None:
        return None
    entry = {
        "tmp": tmp,
        "stub_tmp": None,
        "content": content,
        "lossless": bool(lossless),
        "tokens": max(1, len(content) // 3),
        "source": source or "",
        "bytes": len(content.encode("utf-8", "ignore")),
    }
    old = None
    evicted = []
    with _local_lock:
        old = _local_entries.pop(key, None)
        if old is not None:
            _local_nbytes -= old.get("bytes", 0)
        _local_entries[key] = entry
        _local_nbytes += entry["bytes"]
        while _local_entries and (
            len(_local_entries) > _LOCAL_MAX_ENTRIES or _local_nbytes > _LOCAL_MAX_BYTES
        ):
            _, victim = _local_entries.popitem(last=False)
            _local_nbytes -= victim.get("bytes", 0)
            evicted.append(victim)
    if old is not None:
        _local_unlink(old)
    for victim in evicted:
        _local_unlink(victim)
    _local_hook_exit()
    return entry


def _local_count(path_str: str, tokens: int, source: str) -> None:
    """Queue one served local hit; flush in batches so counters stay honest."""
    global _local_timer
    if not _local_enabled():
        return
    agent_id = _local_agent()
    batch = None
    with _local_lock:
        _local_pending.append(
            {
                "path": path_str,
                "tokens": int(tokens),
                "source": source or "",
                "client": "",
                "agent_id": agent_id,
            }
        )
        if len(_local_pending) >= _LOCAL_FLUSH_AT:
            batch = list(_local_pending)
            _local_pending.clear()
        elif _local_timer is None:
            try:
                timer = threading.Timer(_LOCAL_FLUSH_DELAY_S, _local_flush_pending)
                timer.daemon = True
                _local_timer = timer
                timer.start()
            except Exception:
                _local_timer = None
    if batch:
        _local_flush(batch)


def _local_flush_pending() -> None:
    global _local_timer
    with _local_lock:
        _local_timer = None
        if not _local_pending:
            return
        batch = list(_local_pending)
        _local_pending.clear()
    _local_flush(batch)


def _local_flush(batch) -> None:
    """Send a counter batch. Best-effort: never raises, never retries."""
    tr = _TR
    send = tr.get("send") if isinstance(tr, dict) else None
    if send is None:
        tr = _get_tr()
        send = tr.get("send") if isinstance(tr, dict) else None
    if send is None:
        return
    try:
        resp = send({"cmd": "local_hits", "entries": batch})
    except Exception:
        return
    if isinstance(resp, dict):
        err = resp.get("error")
        if err and "unknown command" in str(err).lower():
            _local_disable_unsupported()


def _local_disable_unsupported() -> None:
    """Daemon predates ``local_hits``: revert this process to the IPC path.

    Reads stay correct either way, but staying on the fast path would make the
    counters skip every local hit — slower and honest beats fast and blind.
    Happens once per process (the flag is sticky).
    """
    global _local_supported
    _local_supported = False
    try:
        local_cache_reset()
    except Exception:
        pass


def _serve_local(entry, path_str: str, mode, mode_kind: str, args, kwargs):
    """Serve ``entry`` like ``open()`` would. None = fall through."""
    content = entry.get("content") or ""
    if mode_kind == "binary":
        if not entry.get("lossless"):
            return None
        serve_mode = "rb"
        kwargs = {k: v for k, v in kwargs.items() if k not in ("encoding", "errors", "newline")}
    else:
        serve_mode = mode
        enc = kwargs.get("encoding")
        # The kept file is written utf-8; any other requested encoding must go
        # through the per-call materialiser so the bytes still decode.
        if enc is not None and str(enc).lower().replace("_", "-") not in ("utf-8", "utf8"):
            return None
    if _stub_maybe is not None and entry.get("source") == "agent_tool":
        try:
            note = _stub_maybe(
                path_str,
                content,
                is_agent=_process_is_agent(),
                lossless=bool(entry.get("lossless")),
            )
        except Exception:
            note = None
        if note is not None:
            stub_tmp = entry.get("stub_tmp")
            if not stub_tmp:
                stub_tmp = _local_materialise(note)  # bytes must stay str
                entry["stub_tmp"] = stub_tmp
            if stub_tmp:
                # Open BEFORE counting: the flush can downgrade the cache for
                # an old daemon, which unlinks the kept temp files — a handle
                # already open survives that (POSIX), a path lookup does not.
                handle = _original_open(stub_tmp, serve_mode, *args, **kwargs)
                _local_count(path_str, entry.get("tokens", 0), entry.get("source", ""))
                return handle
    handle = _original_open(entry["tmp"], serve_mode, *args, **kwargs)
    _local_count(path_str, entry.get("tokens", 0), entry.get("source", ""))
    return handle


# ─── CONTEXT-tier source tagging (shim reads as agent_tool) ───
#
# Problem being fixed: every shim-served cache hit was tagged
# source="shim", which counts toward disk-read avoidance but NEVER
# toward tokens_not_sent_to_llm (the CONTEXT tier). Yet in an agent
# process the shim is exactly how the agent's file reads are served —
# the content it returns IS the LLM's context. The CONTEXT tier was
# measuring ~nothing on shim-installed agents.
#
# The over-counting danger in the other direction: a shim hit does not
# automatically enter LLM context. Libraries vendored in site-packages,
# binary blobs, package metadata, and multi-MB data files are read by
# processes that merely RUN INSIDE an agent — tagging all of them as
# context savings would overstate the tier (the same inflation class as
# the 2026-09-10 tracker fix: 380MB of session JSONs counted as
# "droppable context"). So the tag is a conservative AND-gate, failed
# closed to "shim": only agent-process + human-code-shaped + text +
# small files count.

# Env-var markers that say "this process is an agent harness" (checked
# as prefixes; verified live: Hermes gateway/webui both set HERMES_*).
# Zero-config AUTO-DETECT fallback for agent processes. NOT the gate: the
# supported contract is TOOLRECALL_AGENT_ID / TOOLRECALL_AGENT_SESSION (see
# _agent_identity). A harness missing from this list is covered by the
# contract; adding a vendor here is optional convenience, never required.
_AGENT_ENV_MARKERS = (
    "HERMES_",
    "CLAUDECODE",
    "CLAUDE_CODE_ENTRYPOINT",
    "CURSOR_TRACE_ID",
    "AIDER_MODEL",
    "WINDSURF_",
    "CODEIUM_",
    "TRAE_",
    "GITHUB_COPILOT",
    "COPILOT_CLI",
    "REPLIT_",
    "CODEX_",
    "OPENCODE_",
    "CODEBUDDY_",
    "QODO_",
)

# Directory markers for code the agent runs WITH, not code it authored.
# Reads from these trees almost never enter LLM context.
_LIBRARY_DIR_MARKERS = (
    "/site-packages/",
    "/dist-packages/",
    "/node_modules/",
    "/.venv/",
    "/venv/",
    "/virtualenvs/",
    "/__pycache__/",
    "/.tox/",
    "/.nox/",
    "/.mypy_cache/",
    "/.pytest_cache/",
    "/.ruff_cache/",
    "/.git/",
    "/vendor/",
    "/.hermes/",  # agent home data (sessions, cache) — daemon-side reads
)

# Binary-ish suffixes: content the model would never read as text.
_BINARY_SUFFIXES = frozenset(
    {
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".bmp",
        ".ico",
        ".webp",
        ".tiff",
        ".pdf",
        ".ps",
        ".eps",
        ".zip",
        ".gz",
        ".bz2",
        ".xz",
        ".7z",
        ".rar",
        ".tar",
        ".tgz",
        ".exe",
        ".dll",
        ".so",
        ".dylib",
        ".bin",
        ".o",
        ".a",
        ".obj",
        ".woff",
        ".woff2",
        ".ttf",
        ".otf",
        ".eot",
        ".db",
        ".sqlite",
        ".sqlite3",
        ".duckdb",
        ".parquet",
        ".mp3",
        ".mp4",
        ".mkv",
        ".avi",
        ".mov",
        ".flac",
        ".ogg",
        ".wav",
        ".pyc",
        ".pyo",
        ".class",
        ".wasm",
        ".jar",
        ".whl",
        ".egg",
        ".doc",
        ".docx",
        ".xls",
        ".xlsx",
        ".ppt",
        ".pptx",
        ".iso",
        ".img",
        ".dmg",
        ".deb",
        ".rpm",
    }
)

# Cap on file size that plausibly enters LLM context. Reads larger than
# this are clamped by every harness's read tool (Hermes clamps hard); a
# multi-MB cache hit is data, not context.
MAX_AGENT_CONTEXT_BYTES = 256 * 1024

_agent_process_cache: bool | None = None
_tag_source_mode_cache: str | None = None


def _agent_identity() -> str | None:
    """Resolve the harness-neutral agent identity for this process.

    The supported contract (docs/AGENT_TRACKING.md): an integration exports
    ``TOOLRECALL_AGENT_ID`` (any slug) before it launches the agent, and core
    never needs to know the harness NAME — only that a human put this process
    in an agent session. ``TOOLRECALL_AGENT_SESSION`` is the equivalent
    boolean-style opt-in for integrations that do not want per-agent
    attribution.

    Falsy values (``""``, ``0``, ``false``, ``no``, ``off``) mean "not
    declared", so an integration that disables the contract keeps the
    pre-contract behaviour (no context-tier tagging, no stubs).
    """
    for var in ("TOOLRECALL_AGENT_ID", "TOOLRECALL_AGENT_SESSION"):
        raw = (os.environ.get(var) or "").strip()
        if raw and raw.lower() not in ("0", "false", "no", "off"):
            return raw
    return None


def _env_has_agent_marker() -> bool:
    """True when this process is invocable as an agent session.

    Order — generic contract FIRST, vendor auto-detect second:

      1. ``TOOLRECALL_AGENT_ID`` / ``TOOLRECALL_AGENT_SESSION``: the
         harness-neutral opt-in any adapter, framework or hand-rolled loop can
         set. This is the supported path and needs no framework knowledge in
         core (design principle: Agnostic).
      2. ``_AGENT_ENV_MARKERS``: zero-config auto-detect for the harnesses
         listed there. Fallback ONLY — a harness that is not listed, or a fork
         that renames its env, is covered by (1) instead of requiring a core
         edit. Before this split the list was the sole gate, so an unlisted
         harness silently got no stubs and no CONTEXT-tier accounting
         (measured 2026-11-11: 51 stub events in 61 days on a host whose
         harness happened to be listed, zero on anything else).
    """
    if _agent_identity() is not None:
        return True
    for key in os.environ:
        for marker in _AGENT_ENV_MARKERS:
            if key.startswith(marker):
                return True
    return False


def _process_is_agent() -> bool:
    """Cached once per process: does this process belong to an agent harness?"""
    global _agent_process_cache
    if _agent_process_cache is None:
        try:
            _agent_process_cache = _env_has_agent_marker()
        except Exception:
            _agent_process_cache = False
    return _agent_process_cache


def _tag_source_mode() -> str:
    """Resolve the tagging mode: TOOLRECALL_SHIM_TAG_SOURCE env > [shim].tag_source TOML > "auto".

    Modes: "auto" (gate below), "always" (force agent_tool — for agent
    harnesses whose env markers this build doesn't know), "never" (old
    behavior: shim hits never count toward the CONTEXT tier).
    Invalid values fall back to "auto".
    """
    global _tag_source_mode_cache
    if _tag_source_mode_cache is None:
        raw = (os.environ.get("TOOLRECALL_SHIM_TAG_SOURCE") or "").strip().lower()
        if not raw:
            try:
                from toolrecall.config import load_config

                raw = str(load_config().get("shim", "tag_source", default="auto")).strip().lower()
            except Exception:
                raw = "auto"
        if raw not in ("auto", "always", "never"):
            raw = "auto"
        _tag_source_mode_cache = raw
    return _tag_source_mode_cache


def _looks_like_library_path(path: str) -> bool:
    """True for vendored/package trees — run by the process, not read by the model."""
    norm = path.replace(os.sep, "/")
    # Markers are matched against the path RELATIVE to the tempdir: when
    # TMPDIR nests inside an agent-home tree (Hermes scratch:
    # TMPDIR=~/.hermes/cache/scratch), the tempdir prefix itself satisfies
    # position-independent markers (/.hermes/) and agent-authored ephemeral
    # files classify "shim". Library STRUCTURE inside the tempdir
    # (simulated venvs: <tmp>/site-packages/...) still matches after the
    # strip — only the tempdir's own path stops counting.
    try:
        import tempfile

        tmp = os.path.realpath(tempfile.gettempdir())
        real = os.path.realpath(path)
        if real == tmp or real.startswith(tmp + os.sep):
            rel = os.path.relpath(real, tmp).replace(os.sep, "/")
            norm = rel
    except OSError:
        pass
    return any(marker in norm for marker in _LIBRARY_DIR_MARKERS)


def _tag_source_for(path: str) -> str:
    """Decide the cached_read source for a shim read, pre-call.

    Returns "agent_tool" only when the whole AND-gate passes; "shim"
    otherwise (fail closed — under-tagging slightly understates the
    CONTEXT tier, over-tagging would inflate it with tokens that were
    never in a context window).

    Gate (auto mode): agent-process env marker AND not a library path AND
    not a binary suffix AND file size ≤ 256KB. Uses only the path and one
    os.stat — no content read (source must be chosen before cached_read,
    and the gate must not double the open() work). Binary CONTENT (a
    text-suffixed file full of NULs) slips through by design: the suffix
    blocklist catches the real-world cases, and a text-served binary blob
    is bounded by the size cap.
    """
    mode = _tag_source_mode()
    if mode == "never":
        return "shim"
    if mode == "always":
        return "agent_tool"
    # ── auto gate (AND-gate, fail closed) ──
    if not _process_is_agent():
        return "shim"
    if _looks_like_library_path(path):
        return "shim"
    suffix = os.path.splitext(path)[1].lower()
    if suffix in _BINARY_SUFFIXES:
        return "shim"
    try:
        _within_cap = os.path.getsize(path) <= MAX_AGENT_CONTEXT_BYTES
    except OSError:
        # stat failed — cached_read will surface the real error for this
        # path; tagging it "shim" vs "agent_tool" is moot either way.
        _within_cap = True
    if not _within_cap:
        return "shim"
    return "agent_tool"


# ─── Internal infrastructure paths to skip (never benefit from caching) ───
# These are loaded from toolrecall.toml [shim].exclude_prefixes (or
# TOOLRECALL_SHIM_EXCLUDE_PREFIXES env var) on first call to _should_skip().
# Files matching these prefixes bypass the shim and go directly to the
# real open() — they are tiny, rewritten constantly, and never benefit
# from caching. Intercepting them just pollutes the cache stats with noise.
# Empty list = bypass NOTHING (all open() calls go through the shim).
_SKIP_PREFIXES = None


def _load_skip_prefixes():
    """Load exclude prefixes from config. Call once on first use."""
    global _SKIP_PREFIXES
    if _SKIP_PREFIXES is not None:
        return
    try:
        from toolrecall.config import load_config

        cfg = load_config()
        prefixes = list(cfg.shim_exclude_prefixes)
    except Exception:
        # Config load failed — fall back to the same built-in defaults the
        # Config property carries. /proc//sys//dev/ must NEVER be intercepted
        # even when config is unreadable: these reads can't be cached and the
        # resulting daemon RPC just produces a denial warning. The agent home is
        # resolved the way Config.agent_home does, so control-plane reads stay
        # out of the shim even with a broken config.
        home = (
            os.environ.get("AGENT_HOME")
            or os.environ.get("TOOLRECALL_AGENT_HOME")
            or os.path.expanduser("~/.hermes")
        ).rstrip("/")
        prefixes = ["/proc/", "/sys/", "/dev/"]
        if home:
            prefixes.append(home + "/")
    # Normalize: entries without a trailing slash would match /procself etc.
    # ("/proc" must not prefix-match "/procself/status"). A bare "/" entry
    # would skip everything — drop it.
    _SKIP_PREFIXES = [p if p.endswith("/") else p + "/" for p in prefixes if p and p != "/"]


def _should_skip(path: str | bytes | os.PathLike) -> bool:
    """Check if a path is an internal infrastructure file that should bypass the shim."""
    if _SKIP_PREFIXES is None:
        _load_skip_prefixes()
    assert _SKIP_PREFIXES is not None
    ps = os.fspath(path)
    for prefix in _SKIP_PREFIXES:
        if ps.startswith(prefix):
            return True
    return False


# ─── Patch open() ───
_original_open = builtins.open


def _open_cached_content(content, mode="r", *args, **kwargs):
    """Return a *real* file object backed by the cached ``content`` str.

    The shim must hand back something that behaves like a normal
    ``open(path, *args, **kwargs)`` result: consumers legitimately rely on
    ``.fileno()``, ``.buffer``, ``.name``, ``os.fstat()``, mmap, select, or
    piping the handle to a subprocess. A plain ``io.StringIO`` provides none
    of that and crashes terminal/bash-style callers, so on a cache hit we
    materialize the cached bytes into a temporary file and return a real
    handle. The temp file is unlinked immediately after open; on POSIX the
    inode stays alive until the returned handle is closed.

    Any exception (disk full, encoding error, ...) propagates so the caller's
    ``except`` falls back to reading the original path directly.
    """
    import tempfile

    fd = None
    tmp_path = None
    try:
        fd, tmp_path = tempfile.mkstemp(prefix=".toolrecall-shim-", suffix=".tmp")
        # Persist cached bytes with the same encoding the re-opened handle
        # will use, so the round-trip is lossless for any encoding.
        # newline="" keeps \n and \r\n EXACTLY as stored — default text-mode
        # write translation would corrupt CRLF content byte-exactness.
        enc = kwargs.get("encoding") or "utf-8"
        with os.fdopen(fd, "w", encoding=enc, newline="") as f:
            f.write(content)
        fd = None  # ownership moved into the with-block
        handle = _original_open(tmp_path, mode, *args, **kwargs)
        try:
            os.unlink(tmp_path)  # POSIX: inode lives until handle is closed
        except OSError:
            pass
        return handle
    except Exception:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        raise


def _read_mode_kind(mode: str) -> str | None:
    """Classify an ``open()`` mode for shim interception.

    Returns "text" or "binary" for read-only modes the shim may serve from
    cache; ``None`` for everything else (write/append/plus-modes, exotic
    flags). ``open()`` accepts the b/t chars in any order ("rb" == "br").
    """
    if not isinstance(mode, str):
        return None
    if "+" in mode or "w" in mode or "a" in mode or "x" in mode:
        return None
    base = mode.replace("b", "").replace("t", "").strip()
    if base != "r":
        return None
    return "binary" if "b" in mode else "text"


# Suffix ALLOWLIST for binary-mode ("rb") serving. The cache stores decoded
# UTF-8 text (errors="ignore"), so an "rb" hit round-trips bytes exactly only
# for true-text files; a binary blob re-encoded from that text is corrupt.
# Allowlist, not blocklist: extensionless/unknown suffixes are exactly where
# binary blobs hide, so they must fall through to the real open(). Hermes'
# native read_file opens "rb" (one chunked pass) — without this gate every
# local agent read bypassed cache + context tracker (2026-09-15).
_TEXT_SUFFIXES = frozenset(
    {
        ".py",
        ".pyw",
        ".md",
        ".txt",
        ".json",
        ".yaml",
        ".yml",
        ".toml",
        ".cfg",
        ".ini",
        ".conf",
        ".html",
        ".htm",
        ".js",
        ".mjs",
        ".cjs",
        ".ts",
        ".tsx",
        ".jsx",
        ".css",
        ".scss",
        ".xml",
        ".csv",
        ".tsv",
        ".sh",
        ".bash",
        ".zsh",
        ".sql",
        ".svg",
        ".log",
        ".lock",
    }
)


def _shim_open(path, mode="r", *args, **kwargs):
    # Don't intercept non-file paths (integers = file descriptors,
    # None, or capture objects from test frameworks).
    if not isinstance(path, (str, bytes, os.PathLike)):
        return _original_open(path, mode, *args, **kwargs)

    path_str = os.fspath(path)
    if isinstance(path_str, bytes):
        # Normalize once: the cache key, the source tag and the counter batch
        # all compare paths as str.
        path_str = os.fsdecode(path_str)

    # Re-entrancy guard: if we're already inside a shim call, fall
    # through to the real open() immediately to prevent infinite recursion.
    if _shim_active():
        return _original_open(path_str, mode, *args, **kwargs)

    prev = _enter_shim()
    try:
        # Skip Hermes internal infrastructure files — they're tiny, rewritten
        # constantly, and caching them just pollutes the stats.
        # NOTE: called inside shim scope so any open() triggered by
        # _load_skip_prefixes() is caught by the re-entrancy guard.
        if _should_skip(path_str):
            return _original_open(path_str, mode, *args, **kwargs)

        tr = _get_tr()
        mode_kind = _read_mode_kind(mode)
        # Binary-mode serving ("rb"): Hermes' native read_file fast path
        # opens "rb" — without this branch every local agent read bypassed
        # cache + tracker. Only known-text suffixes are served: the cache
        # holds decoded UTF-8, so re-encoding binary blobs would corrupt them.
        if tr and mode_kind == "binary" and not path_str.lower().endswith(tuple(_TEXT_SUFFIXES)):
            mode_kind = None  # unknown/binary suffix → real open, untracked
        if tr and mode_kind:
            # W2 fast path: a warm in-process entry answers the read with one
            # stat + one open of the kept temp file. No client call at all.
            entry = _local_lookup(path_str)
            if entry is not None:
                handle = _serve_local(entry, path_str, mode, mode_kind, args, kwargs)
                if handle is not None:
                    return handle
            # Circuit breaker: while open, a known-stalled daemon is not asked
            # at all — the caller pays a disk read instead of a socket timeout.
            if not _breaker_allows_read():
                return _original_open(path_str, mode, *args, **kwargs)
            try:
                # Source tagging: a shim hit in an agent process IS the
                # agent's file-read path — tag it agent_tool so the CONTEXT
                # tier (tokens_not_sent_to_llm) counts it, but only when
                # the conservative pre-call gate passes. The gate must run
                # BEFORE the call (source is a cached_read argument, and
                # the call itself records the hit): it uses path shape + one
                # os.stat, never the content. stats stay exactly-once.
                source = _tag_source_for(path_str)
                result = tr["read"](path_str, source=source, timeout=_read_timeout_s())
                # A timeout is not a cache miss: count it, trip the breaker,
                # and serve the real file instead of trusting a fallback path.
                if isinstance(result, dict) and result.get("_daemon_timeout"):
                    _note_read_timeout(path_str)
                    return _original_open(path_str, mode, *args, **kwargs)
                _note_read_ok()
                # Only serve from shim if it was a cache HIT.
                # On cache miss, fall through to _original_open so the
                # real cached_read (from cache.py) reads the file directly
                # and records stats exactly once.
                if (
                    result
                    and result.get("cached", False)
                    and "content" in result
                    # rb serve must be byte-exact: lossless=False entries
                    # (invalid-UTF-8 bytes, legacy CRLF-stripped text) are
                    # NOT re-encodable — the caller gets the real file.
                    and (mode_kind == "text" or result.get("lossless", False))
                ):
                    content = result["content"]
                    # Keep the bytes locally so the NEXT read is answered
                    # in-process. This is the only place entries are born:
                    # a daemon cache hit. (A miss falls through to a real
                    # open and must not populate the shim: the daemon owns
                    # that accounting, exactly once.)
                    entry = _local_store(
                        path_str,
                        content,
                        bool(result.get("lossless", mode_kind == "text")),
                        result.get("source") or source,
                    )
                    if entry is not None:
                        handle = _serve_local(entry, path_str, mode, mode_kind, args, kwargs)
                        if handle is not None:
                            return handle
                    # ── Unchanged-file stub (Lever 1): second+ agent read of
                    # an unchanged file within TTL -> one-line note instead
                    # of full content. Wire-verified not-sent, counted in
                    # the mcp_dedup measured tier. Fail-open: None = serve.
                    #
                    # GATE (2026-09-18, live incident): stub ONLY when the
                    # read was tagged agent_tool. A stub claims "the agent
                    # holds this content from an earlier read" — for
                    # infra/library reads (source="shim": config.yaml,
                    # gateway-internal files, tooling) that claim is FALSE:
                    # the same process re-reads its own config for CORRECT
                    # bytes every time, and serving the note corrupted the
                    # Hermes gateway's YAML parse ("did not find expected
                    # <document start>" in the shim tmp file) and sent it
                    # into config-recovery backups. The mtime+size cache
                    # gate is not enough here: the CACHED content is
                    # correct; the STUB replaces it with a pointer the
                    # consumer never asked for.
                    if _stub_maybe is not None and result.get("source") == "agent_tool":
                        _stub_note = _stub_maybe(
                            path_str,
                            content,
                            is_agent=_process_is_agent(),
                            lossless=bool(result.get("lossless", mode_kind == "text")),
                        )
                        if _stub_note is not None:
                            # Wire-verified not-sent (mcp_dedup measured tier).
                            # Counting lives in shim_stub's FileStubSession
                            # (counter_fn → daemon cmd dedup_tokens) since the
                            # stub_core split — exactly one send per stub, and
                            # the old inline send here is gone.
                            stub_content = _stub_note
                            # NOTE: always pass the STR note —
                            # _open_cached_content writes text ('w') into the
                            # temp file; passing bytes here raised TypeError,
                            # the outer except swallowed it, and the caller
                            # got FULL content while the counter had already
                            # counted (caught live by the Lever-1 e2e).
                            return _open_cached_content(
                                stub_content,
                                "rb" if mode_kind == "binary" else mode,
                                *args,
                                **kwargs,
                            )
                    if mode_kind == "binary":
                        serve_mode = "rb"
                        # encoding/errors/newline are invalid on a binary
                        # handle; buffering is legal ("rb", 0 → raw IO) and
                        # must pass through unchanged.
                        kwargs = {
                            k: v
                            for k, v in kwargs.items()
                            if k not in ("encoding", "errors", "newline")
                        }
                    else:
                        serve_mode = mode
                    return _open_cached_content(result["content"], serve_mode, *args, **kwargs)
            except Exception:
                # The big invisible one: a shim-internal failure (daemon RPC
                # error, decode issue, temp-file failure) used to vanish and
                # every subsequent read of the path silently bypassed the
                # cache. WARNING → stderr via lastResort (the shim never
                # calls setup_logging — per-process file writes from every
                # open() on the box would be wrong); the gateway journal
                # captures it. Logging calls never raise (logging's own
                # handler errors go through handleError), so no guard needed.
                logging.getLogger("toolrecall.shim").warning(
                    "shim cache path failed; falling through to real open: %s",
                    path_str,
                    exc_info=True,
                )
        return _original_open(path_str, mode, *args, **kwargs)
    finally:
        _exit_shim(prev)


def apply(force: bool = False):
    """Apply the shim monkey-patch. Called once on .pth import.

    Patches ONLY builtins.open for read-mode caching. Intentionally does
    NOT touch subprocess.run/Popen — see module docstring (Option B).
    Skips patching when running under pytest (interferes with capture):
    ``force=True`` bypasses that guard for tests that exercise the
    apply/remove mechanics directly.
    """
    if not _ENABLED:
        return
    # Don't patch when running under pytest. sys.modules is the reliable
    # signal: `python -m pytest` sets sys.argv[0] to pytest/__main__.py
    # (basename "__main__.py"), so the old argv-prefix check silently
    # missed that invocation and patched open() for the whole run —
    # including pytest's own capture machinery (exposed 2026-09-15 by
    # a shim regression test whose disk verification read came back
    # from the cache stub).
    if not force and "pytest" in sys.modules:
        return
    if not force and os.environ.get("PYTEST_CURRENT_TEST"):
        return
    _argv = sys.argv[:5] if sys.argv else []
    if _argv and _argv[0] == "-m" and any("pytest" in str(a) for a in _argv[1:]):
        return
    # builtins.open is an overloaded stdlib function; the shim is a plain
    # drop-in callable. (mypy only checks this body now that apply() is
    # annotated — the assignment predates typing here.)
    builtins.open = _shim_open  # type: ignore[assignment]


def remove():
    """Restore the original builtins.open."""
    builtins.open = _original_open


def disable() -> bool:
    """Persistently disable the shim by writing the marker file.

    Returns True on success. Existing processes keep running as-is; new
    Python processes will skip the shim (marker is honored at import).
    """
    try:
        p = _marker_path()
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write("disabled")
        return True
    except OSError:
        return False


def enable() -> bool:
    """Re-enable the shim by removing the persistent disable marker."""
    try:
        p = _marker_path()
        if p and os.path.exists(p):
            os.remove(p)
        return True
    except OSError:
        return False


# ─── Auto-apply on .pth import ───
apply()
