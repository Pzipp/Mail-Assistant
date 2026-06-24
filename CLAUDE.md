# CLAUDE.md — mail-assistant

Regler for Claude Code-sessioner i dette repository.

---

## IMAP — forbudte operationer

Disse IMAP-kommandoer må **aldrig** bruges mod serveren:

- `STORE` — må ikke bruges til at ændre flags (undtagen `\Flagged` som er eksplicit tilladt i koden)
- `EXPUNGE` — må ikke kaldes direkte; bruges kun implicit af `imaplib` efter `CLOSE` ved flytning
- `DELETE` — mails slettes aldrig
- `APPEND` — mails indsættes aldrig

Mails kan **flyttes** (COPY + slet fra kildemappe) men **aldrig slettes endeligt**.

---

## Afsendelse og markering

- Send **aldrig** mails (ingen SMTP, ingen svar, ingen forward)
- Marker **aldrig** en mail som læst (`\Seen`)
- Indsæt **aldrig** mails i IMAP (ingen `APPEND`)

---

## Versionsformat

Toplinjen i docstringen øverst i `assistant.py` skal følge dette format:

```
V: 0.2R{n} — YYYY-MM-DD
```

Eksempel:
```python
"""
V: 0.2R19 — 2026-06-24
mail-assistant — IMAP → LLM → Google Tasks
...
"""
```

- `{n}` er et fortløbende heltal — øg med 1 ved hver ændring af `assistant.py`
- Datoen er den dato hvor ændringen foretages (YYYY-MM-DD)
- Formatet ændres ikke — hverken præfiks, bindestreg eller datoformat

---

## Git — hvad må aldrig committes

| Fil/mappe | Årsag |
|---|---|
| `.env` | Indeholder passwords og API-nøgler |
| `credentials/` | Google OAuth client secrets |
| `data/` | OAuth-token, database med maildata |
| `*.pickle` | Google OAuth-token |
| `*.db` | SQLite-database med behandlede mails |

Kontrollér altid med `git status` og `git diff --cached` inden commit.

---

## Generelle regler

- Rør **ikke** `assistant.py` medmindre opgaven eksplicit kræver det
- Tilføj ikke fejlhåndtering for scenarier der ikke kan opstå
- Skriv ingen kommentarer der beskriver *hvad* koden gør — kun *hvorfor* hvis det ikke er åbenlyst
- Test aldrig mod produktions-IMAP uden eksplicit tilladelse
