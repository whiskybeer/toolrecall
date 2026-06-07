# ToolRecall

[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)]()
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)]()

**Tool-Output Cache für LLM Agents — Zero-Dependency, SQLite-FTS5, Hybrid In-Memory**

ToolRecall cached Tool-Outputs (Datei-Reads, Terminal-Kommandos, Skripte, Code-Execution, MCP-Responses) mit automatischer Invalidierung. Spart Tokens und Ausführungszeit — ohne externe Dependencies (nur Python stdlib).

---

## Was ist ToolRecall? (Und was nicht?)

| ToolRecall ist... | ToolRecall ist KEIN... |
|------------------|----------------------|
| Eine Python-Bibliothek zum Cachen von Tool-Outputs | Distributed Cache (single-node only) |
| Zero-Dependency (Python stdlib only) | Plugin-System (gibts nicht) |
| SQLite-FTS5 + In-Memory-LRU (hybrid) | Vector-DB / Embedding-basiert |
| **MCP-Server** (`toolrecall mcp`) — exponierte Cache-Funktionen als MCP-Tools | **HTTP-Proxy** (proxy.py als optionales Add-on, kein Kernteil) |
| Plug-and-Play für Hermes und andere Python-Agents | Nur für Claude Code — funktioniert mit jedem MCP-kompatiblen Client

> **MCP-Integration:** Zwei Modi:
> 1. **MCP-Server** (`toolrecall mcp`) — exponierte Cache-Funktionen (`cached_read`, `cached_terminal`, `cached_skill`, `docs_search`) als MCP-Tools. Hermes kann via `mcp_servers` verbinden.
> 2. **MCP-Response-Cache** (`cached_mcp`) — cached **Responses von MCP-Tool-Calls** anderer MCP-Server (z.B. fetch, time, github). TTL-basiert, 13.5× Speedup.

---

## Was wird gecached?

| API | Cached? | Invalidierung | Backend |
|-----|---------|---------------|---------|
| `cached_read(path)` | ✅ Datei-Reads | **mtime**: nur bei Änderung neu lesen | In-Memory LRU (0.001ms) + SQLite (Cross-Session) |
| `cached_skill(name)` | ✅ Skill-Inhalte | **mtime**: neuester File-Timestamp im Skill-Ordner | In-Memory + SQLite |
| `cached_terminal(cmd)` | ✅ Bestimmte Terminal-Kommandos | **TTL** (Default: 30s, konfigurierbar) — **exact-match** auf Whitelist | SQLite |
| `cached_run(script, args)` | ✅ Skript-Ausführung | **mtime + TTL**: nur wenn Script unverändert UND TTL frisch | SQLite |
| `cached_exec(code)` | ✅ Python-Code-Execution | **Content-Hash + TTL**: gleicher Code → Cache-Treffer | SQLite |
| `cached_mcp(server, tool, args, fetch_fn)` | ✅ MCP-Tool-Responses | **TTL** (Default: 60s) | SQLite |
| `cached_mcp_check()` / `cached_mcp_store()` | ✅ Low-Level MCP Cache | **TTL** (Default: 60s) | SQLite |
| `docs_search(query)` | 🔍 FTS5-Volltextsuche | Kein Cache (direkte Suche) | SQLite FTS5 (BM25, Porter Stemming) |

### Terminal-Whitelist (Default)

Folgende Kommandos werden gecached — **exact match**:

| Kommando | TTL | Kommando | TTL |
|----------|-----|----------|-----|
| `git status` | 30s | `hostname` | 3600s |
| `git log --oneline -5` | 30s | `whoami` | 3600s |
| `git branch` | 60s | `pwd` | 3600s |
| `git diff --stat` | 30s | `uname -a` | 3600s |
| `crontab -l` | 3600s | `uptime` | 300s |
| | | `free -h` | 300s |
| | | `df -h /` | 300s |
| | | `ls -la` | 60s |

Alle anderen Kommandos (z.B. `git push`, `rm`, `curl`, `apt install`) werden **immer** ausgeführt — kein Cache, kein Delay.

---

## Was wird NICHT gecached?

| Operation | Warum nicht? |
|-----------|-------------|
| **State-changing Terminal-Kommandos** (`git push`, `rm -rf`, `apt install`, `curl POST`, `docker`) | Würden stale/gefährliche Ergebnisse liefern |
| **Nicht-whitelistete Terminal-Kommandos** (Default: alles außer ~15 read-only-Befehle) | Sicherheit > Komfort — jeder Agent muss explizit sagen "das will ich cachen" |
| **HTTP-Requests / API-Calls** (`curl`, `wget`, `requests.get()` ohne MCP-Wrapper) | Nur per `cached_mcp()` mit eigenem `fetch_fn` cachable |
| **State-changing MCP-Tools** (z.B. GitHub Issues erstellen, Linear-Tickets anlegen) | `ttl=0` → bypass |
| **Zufällige/volatile Outputs** (Timestamps, `date`, `curl https://api.weather.gov`) | Würden nie treffen |
| **Große Dateien >10 MB** | LRU-only (kein SQLite-Persist) — spart Disk-I/O |
| **Cross-Session ohne mtime-Änderung** | `cached_read` persistiert in SQLite → Wiederstart-tauglich |
| **Web-Suche** | Kein eingebauter Web-Search — nutze `cached_mcp("fetch", ...)` |

---

## Quick Install

```bash
pip install toolrecall
```

Zero external dependencies — Python stdlib only.

### Hermes Agent Setup

```bash
bash <(curl -s https://raw.githubusercontent.com/whiskybeer/toolrecall/main/setup.sh)
```

Oder manuell:
```bash
pip install toolrecall
hermes config set agent.init_scripts '["~/.toolrecall/hermes_init.py"]'
```

---

## Python Usage

```python
from toolrecall import (
    cached_read,           # Datei-Reads mit Cache
    cached_skill,          # Skill-Inhalte mit Cache
    cached_terminal,       # Terminal-Kommandos (Whitelist)
    cached_run,            # Skript-Ausführung (mtime + TTL)
    cached_exec,           # Python-Code (Content-Hash + TTL)
    cached_mcp,            # MCP-Responses (check → fetch → store)
    docs_search,           # FTS5-Volltextsuche
)

# Datei-Reads (mtime-basiert)
content = cached_read('/path/to/file.md')

# Terminal-Kommandos (exact-match Whitelist)
result = cached_terminal('git status', ttl=30)

# MCP-Responses (optional: eigener fetch_fn)
data = cached_mcp("fetch", "fetch", {"url": "https://..."},
                  fetch_fn=lambda: my_fetcher())

# Volltextsuche (BM25, keine Embeddings)
info = docs_search('how does feature X work')
```

---

## Safety: When NOT to cache

**Jede Cache-Funktion kann mit `ttl=0` umgangen werden:**

```python
# State-changing → immer ausführen
result = cached_terminal('git push origin main', ttl=0)
result = cached_exec('db.delete_all()', ttl=0)
```

**Faustregel:** Wenn Wiederholen das Ergebnis ändern würde → `ttl=0` setzen.

---

## Benchmark (Real-World)

| Cache Type | Without ToolRecall | With ToolRecall |
|-----------|-------------------|-----------------|
| `cached_read` (10K file) | ~10.000 Tokens + 7ms SQLite | **~0 Tokens + 0.001ms** (In-Memory) |
| `cached_terminal` (30s cmd) | ~500 Tokens + 30s | **~0 Tokens + 0.1ms** |
| `cached_run` (5s script) | ~1000 Tokens + 5s | **~0 Tokens + 0.1ms** |
| `cached_exec` (0,5s code) | ~200 Tokens + 0,5s | **~0 Tokens + 0.1ms** |
| `cached_mcp` (API call) | ~500 Tokens + 2s | **~0 Tokens + 0.1ms** |

**Wichtig:** Erster Read ist immer ein Miss (1x zahlen). Erst ab dem 2. Read spart man Tokens.

Token-Schätzung: `len(content) // 3` (gewichteter Durchschnitt aus Code ~2 char/token + English ~4 char/token).

---

## CLI

| Command | Description |
|---------|-------------|
| `toolrecall status` | Cache-Status und Stats |
| `toolrecall stats` | Detaillierte Stats (JSON) |
| `toolrecall invalidate` | Alle Caches leeren |
| `toolrecall index` | Knowledge-Database bauen |
| `toolrecall serve` | HTTP-Proxy starten (optional) |
| `toolrecall mcp` | **MCP-Server starten** — exponiert Cache-Funktionen als MCP-Tools |

---

## MCP Server (Neu ab v0.2.0)

ToolRecall kann als MCP-Server laufen und seine Cache-Funktionen als MCP-Tools exponieren.

### 🏆 Empfehlungshierarchie

| Level | Methode | Für wen | Sicherheit |
|-------|---------|---------|------------|
| 🥇 **Level 1** | **Python-Import** (`hermes_init.py`) | Hermes-Python-Agents | 🔒 Keine Netzwerk-Exposition, volle API |
| 🥈 **Level 2** | **MCP-Server** (`toolrecall mcp`) | MCP-kompatible Agents (Hermes, etc.) | 🔒 stdio-local, path-whitelist, terminal OFF |
| 🥉 **Level 3** | **HTTP-Proxy** (`toolrecall serve`) | Claude Code, Codex, Cursor | ⚠️ Netzwerk-exponiert, nginx+auth empfohlen |

**Empfehlung:** Level 1 (Python-Import) ist am schnellsten und sichersten. Wenn MCP benötigt wird → Level 2 über Hermes `mcp_servers`. Level 3 nur für Agents ohne Python-Import-Fähigkeit.

### Security-Modell

ToolRecall liefert im MCP-Modus **nur sichere Tools standardmäßig aus**. Risikobehaftete Funktionen müssen per Config explizit freigeschaltet werden.

| Tool | Default | Risiko | Warum |
|------|---------|--------|-------|
| `docs_search` | ✅ **Immer** | 🟢 Keines | Reine FTS5-Suche über indizierte Dokumente |
| `docs_get_page` | ✅ **Immer** | 🟢 Keines | Nur indizierte Pages |
| `cached_skill` | ✅ **Immer** | 🟢 Gering | Nur Skill-Dateien lesen |
| `cache_status` | ✅ **Immer** | 🟢 Keines | Nur Statistik-Abfrage |
| `cached_read` | ✅ **Immer** | 🟡 **Path-whitelisted** | Liest nur aus `~/.hermes/skills`, `~/.hermes/scripts`, `~/.toolrecall` |
| `cached_terminal` | ❌ **DEAKTIVIERT** | 🔴 **KRITISCH** | Shell-Zugriff über MCP umgeht alle lokalen Sicherheits-Guards |
| `cache_invalidate` | ❌ **DEAKTIVIERT** | 🟠 Destruktiv | Ein böswilliger Agent könnte den Cache leeren |

> **Warum ist `cached_terminal` standardmäßig deaktiviert?**
> MCP-Tools werden direkt vom Agent aufgerufen — ohne Prompt-Confirmation, ohne Terminal-Whitelist-Check.
> Ein kompromittierter oder falsch konfigurierter Agent könnte `cached_terminal("rm -rf /")` ausführen.
> Selbst der erste Call eines gecachten Befehls wird AUSGEFÜHRT (TTL-Cache greift erst ab dem 2. Call).

### Konfiguration

Die MCP-Security wird in `~/.toolrecall/toolrecall.toml` konfiguriert:

```toml
[mcp]
# Nur diese Pfade dürfen via cached_read gelesen werden
# Leer = ALLE Pfade erlaubt (⚠️ DANGEROUS)
allowed_paths = [
    "~/.hermes/skills",
    "~/.hermes/scripts",
    "~/.toolrecall",
]

# cached_terminal freischalten (⚠️ SECURITY RISK)
allow_terminal = false

# cache_invalidate freischalten
allow_invalidate = false
```

Umgebungsvariablen überschreiben die Config:
```bash
TOOLRECALL_MCP_ALLOWED_PATHS="~/.hermes/skills,~/.hermes/scripts"
TOOLRECALL_MCP_ALLOW_TERMINAL=true   # Nur wenn wirklich nötig!
TOOLRECALL_MCP_ALLOW_INVALIDATE=true
```

### 7 MCP-Tools (vollständig)
| Tool | Beschreibung |
|------|-------------|
| `cached_read(path)` | Datei-Reads mit hybridem LRU+SQLite-Cache |
| `cached_terminal(command, ttl)` | Terminal-Kommandos mit TTL-Cache |
| `cached_skill(name)` | Skill-Inhalte mit Cache |
| `docs_search(query, source)` | FTS5-Volltextsuche (BM25, Porter Stemming) |
| `docs_get_page(source, path)` | Indizierte Seite abrufen |
| `cache_status()` | Cache-Statistiken (Hits/Misses/Tokens) |
| `cache_invalidate()` | Alle Caches leeren |

### Starten

```bash
# Direkt (wenn toolrecall pip-installiert ist)
toolrecall mcp

# Oder via uv (aus dem Repo)
uv run python -m toolrecall.mcp_server
```

### In Hermes config.yaml registrieren (empfohlen)

```yaml
mcp_servers:
  toolrecall:
    command: "uv"
    args: ["run", "python", "-m", "toolrecall.mcp_server"]
    timeout: 30
```

**Beste Praxis (Robins Config):**

```yaml
mcp_servers:
  toolrecall:
    command: "toolrecall"
    args: ["mcp"]
    timeout: 30
    # Kein env-Block nötig — Config aus toolrecall.toml
    # Sensible Daten NIE in mcp_servers.env ablegen!
```

> **Sicherheitswarnung:** Hermes filtert per Default die Umgebungsvariablen für MCP-Subprozesse.
> Nur explizit via `env:` gesetzte Variablen werden durchgereicht. ToolRecall verwendet
> stattdessen `TOOLRECALL_*` Umgebungsvariablen oder die `toolrecall.toml` Config.
> **Niemals API-Keys, Tokens oder Secrets in der `env:`-Sektion von `mcp_servers` ablegen.**

### FastMCP vs. Fallback

| Feature | FastMCP | Fallback (raw stdio) |
|---------|---------|---------------------|
| Voraussetzung | `pip install toolrecall[mcp]` | Keine extra Deps |
| Protokoll | MCP 2024-11-05 (JSON-RPC) | MCP 2024-11-05 (JSON-RPC) |
| Beschreibungen | ✅ Detaillierte Tool-Docs | ✅ Gleichwertig |
| Lifecycle | ✅ Auto-Inject in Hermes Toolset | ✅ Gleichwertig |
| Zusatzfeatures | Progress, Logging, Sampling | Nur tools/list + tools/call |

---

## Module Map

```
toolrecall/
├── __init__.py     # Public API exports (7 Cache-Funktionen + 1 Search)
├── cache.py        # Core caching logic (hybrid LRU+SQLite)
├── cli.py          # CLI entry points (status, stats, invalidate, index, serve, **mcp**)
├── config.py       # TOML config loader
├── config.toml     # Default configuration
├── docs.py         # FTS5 full-text search engine (BM25, Porter stemming)
├── hermes_init.py  # Hermes auto-cache init script
├── mcp_server.py   # **MCP-Server** (FastMCP + raw stdio Fallback)
└── proxy.py        # Optional HTTP proxy (Python stdlib http.server)
```

---

## License

MIT
