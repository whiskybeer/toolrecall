# Hermes Transparent Cache Mode

> English version below. German version first as requested.

## Warum Standard "separate" ist (und warum das viele nicht checken)

ToolRecall installiert sich per `setup.sh` oder `pip install` + `hermes config set` im **"separate"**-Modus:
- Es registriert `cached_read`, `cached_terminal` als *zusätzliche* Tools im Hermes Tool Registry
- Die nativen `read_file`, `terminal` bleiben unverändert
- Problem: **KI-Agenten nehmen fast nie `cached_read`** — sie greifen auf das vertraute `read_file` zurück
- Ergebnis: Der Cache existiert, wird aber nicht getroffen → 0-2 Hits pro Session

Das ist der Grund warum dein Freund "nix sieht" obwohl ToolRecall installiert ist.

## Was "transparent" macht

```python
# Vorher: Agent ruft read_file auf → geht direkt zur Festplatte
# Nachher: Agent ruft read_file auf → geht durch ToolRecall Cache → bei Hit aus SQLite
```

Der `hermes_init.py` patcht beim Session-Start die Handler von `read_file` und `terminal` in
Hermes' Tool Registry. Der Agent ruft weiterhin `read_file` auf — aber die Antwort kommt
aus dem Cache. **Der Agent merkt nichts.**

### Aktivieren

```toml
# ~/.toolrecall/config.toml
[hermes]
transparent_cache = "transparent"
```

Dann Hermes neustarten oder `/reset`.

### Per Env (ohne Config-Änderung)

```bash
TOOLRECALL_HERMES_MODE=transparent hermes
```

### Was du im Startup-Banner siehst

```
==================================================
  ToolRecall Caching Registered
  Tools: cached_read, cached_terminal, cached_write, cached_patch
  Mode:  Separate
  Backend: Daemon (UDS) — shared cache
==================================================
```

Nach transparent: steht `Mode: Transparent` — dann weißt du es funktioniert.

## Risiken

### 1. Cache-Bugs killen native Tools

Wenn der Cache korrupt ist (seltene SQLite-Probleme), bricht `read_file` — nicht nur `cached_read`.
Im "separate"-Modus kannst du auf die nativen Tools ausweichen. Im "transparent"-Modus nicht.

**Recovery:** `rm ~/.toolrecall/cache.db && toolrecall daemon restart`

### 2. Stale Data

Wenn der Daemon mtime-Änderungen nicht korrekt trackt, liest transparenter Modus stale Dateien.
Das passiert z.B. wenn der Daemon seit Stunden läuft und eine Datei geändert wurde,
während der Cache noch den alten Hash hat.

**Recovery:** `toolrecall invalidate` oder Daemon neustarten.

### 3. Hermes API Coupling

Der Patch greift in `tools.registry` ein — eine interne Hermes-API. Wenn Hermes ein Update
bringt das diese API ändert, fliegt der Patch raus und `read_file` returned Fehler.

**Dann:** `[hermes]`-Section aus Config löschen → fällt zurück auf "separate" → läuft wieder.

### 4. Nur Hermes

Transparent-Mode patcht Hermes' Python-internes Tool Registry. Andere Agenten
(Claude Code, Cursor, Cline) nutzen MCP — da gibt es diesen Mechanismus nicht.
Die nutzen immer die expliziten `toolrecall mcp`-Tools.

---

## English

### Why "separate" is default (and why nobody notices)

ToolRecall installs via `setup.sh` or `pip install` in **"separate" mode**:
- It registers `cached_read`, `cached_terminal` as *extra* tools alongside native ones
- Native `read_file`, `terminal` remain unchanged
- Problem: **AI agents almost never pick `cached_read`** — they default to the familiar `read_file`
- Result: cache exists, but 0-2 hits per session

That's why your friend sees "nothing" despite ToolRecall being installed.

### What "transparent" does

The `hermes_init.py` monkey-patches Hermes' tool registry handlers for `read_file` and `terminal`
at session start. The agent still calls `read_file` — but responses come from the cache.
**The agent never notices.**

### Enable

```toml
# ~/.toolrecall/config.toml
[hermes]
transparent_cache = "transparent"
```

Then restart Hermes or `/reset`.

### Env override (no config change)

```bash
TOOLRECALL_HERMES_MODE=transparent hermes
```

### Risks

1. **Cache bugs break native tools.** Recovery: `rm ~/.toolrecall/cache.db && toolrecall daemon restart`
2. **Stale data.** Recovery: `toolrecall invalidate` or restart daemon
3. **Hermes API coupling.** If Hermes updates `tools.registry`, transparent mode breaks. Remove `[hermes]` from config to revert to "separate".
4. **Hermes-only.** Other agents (Claude Code, Cursor, Cline) always use explicit MCP tools.
