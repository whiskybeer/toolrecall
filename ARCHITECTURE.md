# ToolRecall Daemon-Architektur — Vorschlag

## 1. Das Problem

ToolRecall hat heute **drei unabhängige Zugangswege** — jeder mit eigenem Cache-Prozess:

```
┌──────────────┐   ┌──────────────┐   ┌──────────────┐
│  Hermes      │   │  MCP Server  │   │  HTTP Proxy  │
│  (init)      │   │  (mcp)       │   │  (serve)     │
├──────────────┤   ├──────────────┤   ├──────────────┤
│ In-Memory    │   │ In-Memory    │   │ In-Memory    │
│ LRU (~20MB)  │   │ LRU (~20MB)  │   │ LRU (~20MB)  │
├──────────────┤   ├──────────────┤   ├──────────────┤
│ SQLite (WAL) │   │ SQLite (WAL) │   │ SQLite (WAL) │
│  cache.db    │   │  cache.db    │   │  cache.db    │
└──────────────┘   └──────────────┘   └──────────────┘
        ▲                                    
        │ gleiche DB-Datei, aber...
        │ 
   Prozessgrenze ─────────────────────────────
        │ 
   ❌ Jeder startet kalt (leerer LRU)
   ❌ Drei Prozesse = ~60MB RAM
   ❌ ~200ms Startup für MCP/HTTP
   ❌ Caches arbeiten gegeneinander
```

**Problem:** Die drei LRUs sind *nicht synchronisiert*. Hermes cached Datei A in seinen LRU. Der MCP Server hat eine leere LRU und liest Datei A aus SQLite (7ms) — dabei hätte er sie in 0.001ms aus dem Shared Memory haben können.

## 2. Die Lösung: Ein Daemon, drei Brücken

```
                    ╔════════════════════════════╗
                    ║   ToolRecall Daemon        ║
                    ║   (toolrecall daemon)      ║
                    ║                            ║
                    ║   ┌──────────────────┐     ║
                    ║   │  In-Memory LRU   │     ║
                    ║   │  (20MB, warm)     │     ║
                    ║   └────────┬─────────┘     ║
                    ║            │                ║
                    ║   ┌────────▼─────────┐     ║
                    ║   │  SQLite (WAL)    │     ║
                    ║   │  cache.db        │     ║
                    ║   └────────┬─────────┘     ║
                    ║            │                ║
                    ║   ┌────────▼─────────┐     ║
                    ║   │  IPC Server      │     ║
                    ║   │  UDS Socket      │     ║
                    ║   │  /tmp/tc.sock    │     ║
                    ║   └──────────────────┘     ║
                    ╚════════════════════════════╝
                              │ UDS
       ┌──────────────────────┼──────────────────────┐
       │                      │                      │
       ▼                      ▼                      ▼
┌──────────────┐    ┌──────────────┐    ┌──────────────┐
│ Hermes Client│    │  MCP Bridge  │    │  HTTP Bridge │
│              │    │              │    │              │
│ from         │    │ toolrecall   │    │ toolrecall   │
│ toolrecall   │    │ mcp          │    │ serve        │
│ .client      │    │              │    │              │
│ import *     │    │ stdin/stdout │    │ HTTP GET/POST│
│              │    │   → UDS      │    │   → UDS      │
└──────────────┘    └──────────────┘    └──────────────┘
```

### Was passiert genau

**Daemon** (`toolrecall daemon`):
- Ein Python-Prozess, startet beim System (systemd unit)
- Hält LRU + SQLite + UDS-Server
- Verarbeitet Requests: `{cmd: "read", path: "/x"} → {content: "...", cached: true}`
- Läuft tagelang/woechentlich — Cache bleibt warm über Sessions hinweg

**Hermes Client** (`from toolrecall.client import cached_read`):
- Statt eigenem LRU + SQLite: leitet an den Daemon weiter
- `cached_read(path)` → JSON über UDS → Daemon checkt LRU → antwortet
- **Fallback:** Wenn kein Daemon läuft, nutzt direktes SQLite (wie heute)
- hermes_init.py wird minimal (~20 LOC statt 112)

**MCP Bridge** (`toolrecall mcp`):
- Startet sofort (kein Python-Modul-Laden nötig — nur socket + json)
- Liest stdin (JSON-RPC), übersetzt in UDS-Call, schreibt Antwort auf stdout
- **Keine eigene Logik** — nur Protokoll-Übersetzung

**HTTP Bridge** (`toolrecall serve`):
- Selbes Prinzip: HTTP-Request → UDS-Call → HTTP-Response
- Kein eigenes SQLite, kein LRU

## 3. Für wen ist das interessant?

### Gruppe A: Hermes-Nutzer mit ToolRecall (aktuell: Robin)
| Heute | Daemon-Architektur |
|-------|-------------------|
| hermes_init.py lädt cache.py (112 LOC) | Client (20 LOC) |
| ToolRecall startet kalt pro Session | Cache ist immer warm (Daemon läuft seit Tagen) |
| MCP Server braucht extra RAM | MCP Bridge ist <10MB |
| Hermes Restart = Cache kalt | Daemon überlebt Hermes-Neustarts |

**Wert:** spürbarer — vor allem auf e2-medium mit 4GB RAM. Einmal Daemon starten, nie wieder über Caches nachdenken.

### Gruppe B: Entwickler die ToolRecall in eigene Tools einbauen
| Heute | Daemon-Architektur |
|-------|-------------------|
| Müssen `from toolrecall import cached_read` | Können UDS von jeder Sprache nutzen (curl, nc, Go, Rust) |
| Python-only | Jede Sprache → UDS |

**Wert:** ToolRecall wird sprachunabhängig. Ein Go-Service kann denselben Cache nutzen wie ein Python-Script.

### Gruppe C: Claude Code / Cursor / Codex User (Robins Zielgruppe)
| Heute | Daemon-Architektur |
|-------|-------------------|
| MCP Server ist eigener Prozess (200ms Startup) | MCP Bridge startet in <10ms |
| Jeder Claude Code Start = neuer kalter Cache | Daemon läuft, Cache warm |
| Aktuell: "brauch ich nicht weil zu teuer" | "starte ich weil sofort da" |

**Wert:** niedrigere Einstiegshürde. Der Daemon macht ToolRecall auf einer Maschine zur "always-on"-Infrastruktur.

### Gruppe D: CI/CD
| Heute | Daemon-Architektur |
|-------|-------------------|
| Jeder CI-Step startet eigenen Cache | Ein Daemon pro Build-Host |
| Cache wird nie warm (Steps sind kurz) | Cache lebt über Step-Grenzen |

**Wert:** erst in größeren CI-Umgebungen. Für GitHub Actions eher Overkill.

## 4. Warum ist das hier anders?

### Anders als heute

| Aspekt | Heute | Daemon | 
|--------|-------|--------|
| **Architektur** | 3 gleichberechtigte Prozesse | 1 Zentrum + 3 Brücken |
| **Cache-Sharing** | Nur SQLite (7ms) | LRU + SQLite (0.001ms + 7ms) |
| **RAM** | ~60MB (3 × LRU) | ~25MB (1 × LRU + Bridges) |
| **MCP Startup** | ~200ms (uv run python -m ...) | ~5ms (Python stdio → socket) |
| **Sprachbindung** | Nur Python | Jede Sprache via UDS |
| **Fehlertoleranz** | Ein Prozess fällt aus → andere leben | Daemon fällt aus → alle tot (braucht systemd) |
| **Komplexität** | 3 Module nebeneinander | 1 Kern + 3 dünne Bridges |

### Anders als ein HTTP-Proxy

`toolrecall serve` (HTTP Proxy) ist bereits eine Netzwerk-Brücke. Der Unterschied:

- **HTTP Proxy**: HTTP-REST-API, request/response, kein Persistent Connection State, jeder Request authentifiziert sich neu
- **Daemon + UDS**: Unix Domain Socket, persistent connection, ~10× schneller, kein Netzwerk-Stack, nur lokale Kommunikation
- **UDS vs HTTP**: UDS ist ~0.1ms pro Call, HTTP localhost ~0.5ms. UDS hat keine Port-Konflikte, kein Firewall, keine Auth nötig (nur Filesystem-Permissions)

### Anders als direkter Python-Import

Direkter Import (`from toolrecall import cached_read`) ist der schnellste Weg — 0.001ms plus 0ms Overhead. Aber: pro Prozess, kein Sharing.

Die Daemon-Architektur opfert 0.1ms UDS-Overhead für Shared Cache. In der Praxis: 0.1ms ist nichts — LLM-API-Calls dauern 3-10s.

**Die Frage ist nicht "schneller oder langsamer". Die Frage ist: "Nutzt du ToolRecall in einem oder mehreren Prozessen?"**

| Nutzungsszenario | Optimaler Weg |
|-----------------|---------------|
| Ein Prozess (nur Hermes) | Direkter Import — Daemon bringt nichts |
| Mehrere Prozesse (Hermes + MCP + HTTP) | Daemon — sonst 3× RAM + 3× kalt |
| CI/CD / Microservices | Daemon — sonst nie warmer Cache |

## 5. Offene Fragen

1. **systemd unit** — wer managed den Daemon? `toolrecall daemon --install`?
2. **Fallback-Verhalten** — wenn Daemon stirbt, soll cached_read automatisch auf direktes SQLite fallen?
3. **UDS Pfad** — `/tmp/toolrecall.sock` oder `~/.toolrecall/toolrecall.sock`?
4. **Auth** — UDS hat nur Filesystem-Permissions (`chmod 700`). Reicht das?
5. **Multiuser** — wenn zwei User auf der Maschine ToolRecall nutzen, brauchen sie getrennte Sockets?

## 6. Nächste Schritte

Falls interessant:
1. Ich skizziere `daemon.py` (UDS-Server, ~150 LOC)
2. Ich skizziere `client.py` (UDS-Client mit Fallback, ~80 LOC)
3. Ich skizziere Bridge-Rewrites für MCP + HTTP
4. Zeige Diff: 450 LOC weniger als heute