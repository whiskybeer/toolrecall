# Anomaly Report — Session 2026-06-10

Gemessen am aktiven ToolRecall-Repository (~/toolrecall), Cache-DB und Session-DB.

## Python-Code-Qualität

| Prüfung | Ergebnis |
|---------|----------|
| Python Syntax (alle Module + Tests) | ✅ Keine Syntax-Fehler |
| Linting | ⚠️ Nicht gemessen |

**Fazit:** Python selbst ist nicht die Ursache. Alle .py-Dateien kompilieren sauber.

## SQLite-Datenbank-Integrität

| Datenbank | Größe | Tabellen | Integrity |
|-----------|-------|----------|-----------|
| cache.db | 532 KB | 7 | ✅ ok |
| state.db | 341 MB | 18 | ✅ ok |
| messages.db | 0 B | — | ✅ ok (leer) |

**Fazit:** Keine korrupten Datenbanken.

## Cache-DB: Datenqualität

### file_cache (39 Einträge)
- NULL-Spalten: ✅ 0 in allen Spalten
- Duplicate Keys: ✅ 0
- ZERO mtime: ✅ 0
- Ø Content: 10.7 KB, Max: 69.2 KB

### cache_stats
- Hits: 123, Misses: 16 → **Hit-Rate: 88.5%** ✅
- Tokens intercepted: 55,764

### ⚠️ Leere Tabellen (Erwartungsabweichung)
| Tabelle | Status | Erwartet |
|---------|--------|----------|
| skill_cache | ❌ LEER | Sollte Skill-Caches enthalten |
| mcp_cache | ❌ LEER | MCP-Tools nutzen Cache nicht |
| terminal_cache | ❌ LEER | Terminal-Outputs werden nicht gecached |

Diese 3 Tabellen sind leer — kein Korruptions-Fehler, sondern **fehlende Integration**.

## Test-Ergebnisse: Erwartung vs. Realität

| Metrik | Erwartet | Tatsächlich | ❌ Abweichung |
|--------|----------|-------------|--------------|
| Tests bestanden | 176 (alle) | 132 | -44 |
| Tests fehlgeschlagen | 0 | 44 | +44 |
| Security WAF/Injection | ✅ bestanden | ✅ bestanden | — |
| Cognitive Scan | 0 fail | **44 fail** | Ursache identifiziert |

**Ursache der 44 Failures:** `test_cognitive_scan.py` — neues, unfertiges Feature. Kein Laufzeit-Bug.

## Antwort auf deine Frage

> Ist es Python, SQLite oder korrupte Daten?

**Keins davon.** Python und SQLite sind sauber. Die Probleme sind:

1. **44 fehlgeschlagene Tests** — halbfertiges Cognitive-Scan-Feature (Design-Problem)
2. **3 leere Cache-Tabellen** — fehlende Integration (kein Bug, sondern Lücke)
3. **SHM-Autorisierungs-Problem** — Architektur-Frage: SecurityGate lebt im MCP-Prozess, nicht im Cache selbst. Wird durch den Daemon-Ansatz adressiert.
