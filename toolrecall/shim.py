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
import sys
import threading


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
            from .client import cached_read as cr

            _TR = {"read": cr}
        except ImportError:
            _TR = False
    return _TR


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


def _env_has_agent_marker() -> bool:
    """True if any agent-harness env marker is present in this process."""
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
        # resulting daemon RPC just produces a denial warning.
        prefixes = ["/proc/", "/sys/", "/dev/"]
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
        enc = kwargs.get("encoding") or "utf-8"
        with os.fdopen(fd, "w", encoding=enc) as f:
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
            try:
                # Source tagging: a shim hit in an agent process IS the
                # agent's file-read path — tag it agent_tool so the CONTEXT
                # tier (tokens_not_sent_to_llm) counts it, but only when
                # the conservative pre-call gate passes. The gate must run
                # BEFORE the call (source is a cached_read argument, and
                # the call itself records the hit): it uses path shape + one
                # os.stat, never the content. stats stay exactly-once.
                result = tr["read"](path_str, source=_tag_source_for(path_str))
                # Only serve from shim if it was a cache HIT.
                # On cache miss, fall through to _original_open so the
                # real cached_read (from cache.py) reads the file directly
                # and records stats exactly once.
                if result and result.get("cached", False) and "content" in result:
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
                pass
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
